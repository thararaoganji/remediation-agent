"""Phase VI — Enhance Coverage Agent pipeline.

Reuses core's tool-agnostic fix-loop engine (core/agents/fix_loop.py,
core/agents/checkpoint.py, core/agents/outer_loop.py, core/agents/report.py)
rather than re-implementing loop machinery here: PushStep, OuterExitCheck,
PerFileLoopStep, FixLlmGateStep, and the LLM call plumbing
(_llm_error_message/_hide_text/_extract_code_block/_looks_like_diff/
_no_safe_fix_reason) are all genuinely domain-agnostic — none of them know
or care whether the "issue" being worked on is a Sonar rule violation or an
uncovered line. SetupStep and the checkpoint pipeline are shared across all
three Sonar agents (sonar/setup.py, sonar/checkpoint.py) since they're
Sonar-specific but not coverage-specific. Only the fetch/prompt/apply-and-
verify steps below are actually specific to test-coverage generation.

The one previous version of this file (CoverageEnhancerStep) was a
placeholder: no LLM call, `time.sleep(2.0)` standing in for real work, only
ever the single most-uncovered file per run, and a report step with
hardcoded fake numbers (token counts, pass/fail counts) that were never
actually measured. This replaces it with a real per-file loop: every
uncovered file gets a genuine LLM-generated test file, a real compile +
test-run verification, and a checkpoint-gated full build/re-scan safety net
exactly like the autofix agent's own files do."""

import os
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from core import state_schema as sk
from core.adapters.base import get_adapter
from core.agents._shared import _msg
from core.agents.fix_loop import (
    FixLlmGateStep, PerFileLoopStep, _build_fix_llm_agent, _extract_code_block,
    _hide_text, _java_fqcn, _llm_error_message, _looks_like_diff, _no_safe_fix_reason,
)
from core.agents.outer_loop import OuterExitCheck
from core.agents.report import PushStep, _format_duration
from core.tools import git_tools

from techdebt_agent.fix import _build_per_file_loop as _build_techdebt_per_file_loop
from techdebt_agent.maintainability import _scanned_branch
from sonar.checkpoint import build_checkpoint_gate
from sonar.setup import SetupStep
from sonar.tools import sonar_tools
from sonar.tools.sonar_tools import fetch_uncovered_files, get_metric_value
from .prompts import build_coverage_prompt


def _java_test_file_path(file_path: str) -> str:
    """src/main/java/.../Foo.java -> src/test/java/.../FooTest.java. Only
    inserts the Test suffix if the file doesn't already end in one (so a
    file already named ...Test.java — unusual for a production class, but
    not impossible — doesn't get double-suffixed)."""
    path = file_path.replace("\\", "/")
    if "/main/" in path:
        path = path.replace("/main/", "/test/", 1)
    if path.endswith(".java") and not path.endswith("Test.java"):
        path = path[: -len(".java")] + "Test.java"
    return path


class CoverageBaselineStep(BaseAgent):
    """Captures the project's coverage % before this run touches anything,
    so the final report can show a real before/after delta instead of a
    lone final number with nothing to compare it to. branch=source_branch
    (or the project default) -- this run's own branch doesn't exist yet."""
    name: str = "coverage_baseline_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        value = get_metric_value(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], "coverage", s.get("source_branch")
        )
        s["temp:coverage_before"] = float(value) if value is not None else None
        note = f"{s['temp:coverage_before']:.1f}%" if s["temp:coverage_before"] is not None else "unknown"
        yield Event(author=self.name, content=_msg(f"Baseline coverage before this run: {note}."))


class CoverageFetchStep(BaseAgent):
    """Coverage's equivalent of outer_loop.FetchPrioritizeStep — fetches the
    project's uncovered files (already sorted lowest-coverage-first by
    fetch_uncovered_files) and excludes anything this run has already
    completed, flagged, or reverted, same exclusion reasoning as the
    autofix agent's own FetchPrioritizeStep (see its docstring): a file
    already given up on this run shouldn't be silently re-queued forever
    across outer-loop iterations just because it's still "not completed"."""

    name: str = "coverage_fetch_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # s.get("source_branch") if this run targeted a specific branch,
        # else None (the project's default) -- see
        # fetch_issues_and_hotspots()'s docstring in sonar_tools.py for why
        # never the agent's own freshly-created branch: it has no analysis
        # of its own until a checkpoint scans it.
        uncovered = fetch_uncovered_files(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], s.get("source_branch")
        )

        excluded = set(s[sk.FILES_COMPLETED]) | set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) \
            | {f["file"] for f in s[sk.FILES_FLAGGED]}
        remaining = [f for f in uncovered if f["file"] not in excluded]

        s[sk.ORDERED_FILES_REMAINING] = remaining
        yield Event(author=self.name, content=_msg(
            f"Found {len(uncovered)} file(s) with incomplete coverage — "
            f"{len(remaining)} queued to add tests for."
        ))


class CoverageFileFixerStep(BaseAgent):
    """Coverage's equivalent of fix.FileFixerStep — pops the next file
    (kept at index 0 until fully handled, same convention as the autofix
    per-file loop), reads the production file plus any existing test file,
    and writes the prompt fix_llm_gate_step will consume."""

    name: str = "coverage_file_fixer_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        queue = s[sk.ORDERED_FILES_REMAINING]
        if not queue:
            s[sk.FILE_LOOP_DONE] = True
            yield Event(
                author=self.name, content=_msg("No files left in the queue."),
                actions=EventActions(escalate=True),
            )
            return

        entry = queue[0]
        working_dir = s[sk.WORKING_DIR]
        file_abs_path = os.path.join(working_dir, *entry["file"].split("/"))
        with open(file_abs_path, encoding="utf-8") as f:
            file_content = f.read()

        test_file_path = _java_test_file_path(entry["file"])
        test_abs_path = os.path.join(working_dir, *test_file_path.split("/"))
        existing_test_content = None
        if os.path.isfile(test_abs_path):
            with open(test_abs_path, encoding="utf-8") as f:
                existing_test_content = f.read()

        s[sk.CURRENT_FILE_GROUP] = {"file": entry["file"], "test_file": test_file_path, "entry": entry}
        # The BEFORE state of the TEST file (not the production file) —
        # ApplyAndVerifyStep needs this both to know whether the test file
        # pre-existed (so it knows how to revert on failure) and to show a
        # diff of what actually changed.
        s[sk.CURRENT_FILE_CONTENT] = existing_test_content or ""
        s["temp:test_file_pre_existed"] = existing_test_content is not None
        s["temp:fix_prompt"] = build_coverage_prompt(
            file_path=entry["file"],
            file_content=file_content,
            coverage=entry["coverage"],
            uncovered_lines=entry["uncovered_lines"],
            uncovered_conditions=entry["uncovered_conditions"],
            test_file_path=test_file_path,
            existing_test_content=existing_test_content,
        )
        yield Event(author=self.name, content=_msg(
            f"Writing tests for `{entry['file']}` ({entry['coverage']:.1f}% covered, "
            f"{entry['uncovered_lines']} uncovered line(s)) — target: `{test_file_path}`."
        ))


class CoverageApplyAndVerifyStep(BaseAgent):
    """Coverage's equivalent of fix.ApplyAndVerifyStep. 'Apply' here means
    writing the model's full regenerated test file to disk (not a diff —
    see prompts.py's docstring for why full-file output is the primary
    format in this domain, not a fallback); 'verify' means the test file
    compiles and the new test(s) actually pass, then the same checkpoint
    safety net (full build + re-scan) every other file in this run goes
    through via CheckpointGate right after this step."""

    name: str = "coverage_apply_and_verify_step"

    async def _retry_with_compile_error(
        self, ctx: InvocationContext, group: dict, working_dir: str, test_file: str,
        failed_content: str, error: str,
    ) -> AsyncGenerator[Event, None]:
        """One retry, giving the model the actual compiler/test failure
        from its first attempt. Confirmed live: every failure in a real
        29-file run was the model hallucinating another class's API (a
        setter that doesn't exist, an enum constant that doesn't exist, a
        constructor with the wrong argument count) -- unavoidable in a
        design that only ever shows it the ONE production file under
        test, never its collaborators (a DTO/entity/enum it references).
        The compiler error names the exact wrong symbol and the real
        type it belongs to, which is usually enough to self-correct
        without needing that other file's source at all.

        Sets state["temp:coverage_retry_ok"] rather than returning a
        value (async generators can't `return` one)."""
        s = ctx.session.state
        s["temp:coverage_retry_ok"] = False
        entry = group["entry"]
        file_abs_path = os.path.join(working_dir, *entry["file"].split("/"))
        with open(file_abs_path, encoding="utf-8") as f:
            production_content = f.read()

        s["temp:fix_prompt"] = build_coverage_prompt(
            file_path=entry["file"],
            file_content=production_content,
            coverage=entry["coverage"],
            uncovered_lines=entry["uncovered_lines"],
            uncovered_conditions=entry["uncovered_conditions"],
            test_file_path=test_file,
            existing_test_content=s[sk.CURRENT_FILE_CONTENT] or None,
            previous_attempt=failed_content,
            previous_error=error,
        )
        retry_agent = _build_fix_llm_agent()
        llm_call_error = None
        try:
            async for event in retry_agent.run_async(ctx):
                llm_call_error = _llm_error_message(event) or llm_call_error
                yield _hide_text(event)
        except Exception as e:
            llm_call_error = f"{type(e).__name__}: {e}"

        if llm_call_error is not None:
            s["temp:no_safe_fix_reason"] = f"model call failed: {llm_call_error}"
            yield Event(author=self.name, content=_msg(
                f"Retry for `{entry['file']}`'s test file failed: the model call failed ({llm_call_error})."
            ))
            return

        raw = s.get(sk.PROPOSED_DIFF, "")
        no_safe_fix_reason = _no_safe_fix_reason(raw)
        if no_safe_fix_reason is not None:
            s["temp:no_safe_fix_reason"] = no_safe_fix_reason
            yield Event(author=self.name, content=_msg(
                f"Retry for `{entry['file']}`'s test file declined: {no_safe_fix_reason}"
            ))
            return

        content = _extract_code_block(raw).strip()
        if not content or _looks_like_diff(content):
            yield Event(author=self.name, content=_msg(
                f"Retry for `{entry['file']}`'s test file still returned an unusable response — declining."
            ))
            return

        test_abs_path = os.path.join(working_dir, *test_file.split("/"))
        with open(test_abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        s["temp:coverage_retry_ok"] = True
        yield Event(author=self.name, content=_msg(f"Retry for `{entry['file']}`'s test file generated — verifying."))

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        group = s[sk.CURRENT_FILE_GROUP]
        working_dir = s[sk.WORKING_DIR]
        test_file = group["test_file"]
        entry = group["entry"]
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        pre_existed = s.pop("temp:test_file_pre_existed", False)

        def _flag_and_skip(reason: str) -> None:
            s[sk.FILES_FLAGGED].append({"file": entry["file"], "reason": reason})
            s[sk.ORDERED_FILES_REMAINING].pop(0)

        # See fix.py's _llm_error_message docstring — a blocked/failed LLM
        # turn never writes PROPOSED_DIFF, so this must be checked before
        # anything else touches it.
        llm_call_error = s.pop("temp:llm_call_error", None)
        if llm_call_error is not None:
            _flag_and_skip(f"model call failed ({llm_call_error}) — no test was generated")
            yield Event(author=self.name, content=_msg(
                f"Coverage fix for `{entry['file']}` skipped: the model call failed "
                f"({llm_call_error}) — flagged for manual review."
            ))
            return

        raw = s[sk.PROPOSED_DIFF]
        no_safe_fix_reason = _no_safe_fix_reason(raw)
        if no_safe_fix_reason is not None:
            _flag_and_skip(no_safe_fix_reason)
            yield Event(author=self.name, content=_msg(
                f"Coverage fix for `{entry['file']}` declined: {no_safe_fix_reason} — flagged for manual review."
            ))
            return

        content = _extract_code_block(raw).strip()
        if not content or _looks_like_diff(content):
            _flag_and_skip("model returned diff-shaped or empty output instead of a complete test file")
            yield Event(author=self.name, content=_msg(
                f"Coverage fix for `{entry['file']}` failed: the model didn't return a usable test file — "
                "flagged for manual review."
            ))
            return

        test_abs_path = os.path.join(working_dir, *test_file.split("/"))
        os.makedirs(os.path.dirname(test_abs_path), exist_ok=True)
        with open(test_abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        def _discard() -> None:
            if pre_existed:
                git_tools.revert_file(working_dir, test_file)
            else:
                os.remove(test_abs_path)

        # Deliberately NOT adapter.quick_compile_check() here — its Maven/
        # Gradle implementations only reach the `compile`/`compileJava`
        # phase, which never compiles test sources at all (test-compile is
        # a later, separate phase `mvn compile`/`gradle compileJava` never
        # reaches). For a brand-new or substantially-rewritten test file,
        # that check would give zero real signal either way and would
        # falsely "pass" a test file that doesn't even compile.
        # run_specific_tests's `mvn test -Dtest=...`/`gradle test --tests`
        # already compiles main+test sources as a lifecycle dependency of
        # running the test, so it's both the compile check and the
        # behavioral check in one call here.
        test_result = adapter.run_specific_tests(working_dir, [_java_fqcn(test_file)])
        retried = False
        if not test_result.passed:
            yield Event(author=self.name, content=_msg(
                f"New test(s) for `{entry['file']}` failed to compile or pass — retrying with the compiler error."
            ))
            async for event in self._retry_with_compile_error(
                ctx, group, working_dir, test_file, content, test_result.errors[-2000:],
            ):
                yield event
            retried = True
            if s.get("temp:coverage_retry_ok", False):
                test_result = adapter.run_specific_tests(working_dir, [_java_fqcn(test_file)])

        if not test_result.passed:
            _discard()
            no_safe_fix_reason = s.pop("temp:no_safe_fix_reason", None)
            _flag_and_skip(
                no_safe_fix_reason
                or f"generated test(s){' still' if retried else ''} failed to compile or pass: {test_result.errors[-800:]}"
            )
            yield Event(author=self.name, content=_msg(
                f"New test(s) for `{entry['file']}`{' still' if retried else ''} failed to compile or pass — "
                "reverted, flagged for manual review."
            ))
            return

        commit_sha = git_tools.commit(working_dir, f"test: add coverage for {entry['file']}")
        s[sk.FILES_COMPLETED].append(entry["file"])
        s[sk.ISSUES_FIXED].append(f"coverage:{entry['file']}")
        if commit_sha is not None:
            # See fix.ApplyAndVerifyStep's identical comment — a None
            # commit_sha means the regenerated test file was byte-identical
            # to what was already there, nothing new for a checkpoint to
            # revert.
            s.setdefault("temp:checkpoint_batch", []).append({
                "file": test_file, "commit_sha": commit_sha, "issue_keys": [f"coverage:{entry['file']}"],
            })
        s[sk.ORDERED_FILES_REMAINING].pop(0)
        s[sk.FILES_SINCE_CHECKPOINT] += 1
        yield Event(author=self.name, content=_msg(
            f"Added test(s) to `{test_file}` for `{entry['file']}` "
            f"(was {entry['coverage']:.1f}% covered, {entry['uncovered_lines']} uncovered line(s)) — "
            "compiled and passed."
        ))


def _build_coverage_per_file_loop() -> LoopAgent:
    return LoopAgent(
        name="coverage_per_file_loop",
        sub_agents=[
            CoverageFileFixerStep(),
            FixLlmGateStep(llm_agent=_build_fix_llm_agent()),
            CoverageApplyAndVerifyStep(),
            build_checkpoint_gate(),
        ],
        max_iterations=1000,  # real exit is CoverageFileFixerStep's escalate=True on empty queue
    )


coverage_outer_loop = LoopAgent(
    name="coverage_outer_loop",
    sub_agents=[
        CoverageFetchStep(),
        PerFileLoopStep(loop=_build_coverage_per_file_loop()),
        OuterExitCheck(),
    ],
    max_iterations=5,
)


class CoverageQualityGateStep(BaseAgent):
    """Post-pass check, after coverage_outer_loop (and the checkpoint that
    fires when its queue empties has already re-scanned this run's own
    branch): look for new MAINTAINABILITY code smells Sonar found
    specifically in the test file(s) THIS run wrote, and re-queue them for
    a real fix rather than leaving them sitting on the branch. Scoped to
    files in FILES_COMPLETED only -- pre-existing production-code debt
    elsewhere in the project is techdebt_agent's job, not this agent's.

    Reuses techdebt_agent's own per-file loop (FileFixerStep/
    ApplyAndVerifyStep/build_fix_prompt via _build_techdebt_per_file_loop)
    to actually fix what's found -- "fix this Sonar code smell in this
    Java file" is exactly what that machinery already does, well-tested,
    regardless of whether the file happens to be a test file or
    production code. One known gap inherited from that reuse:
    ApplyAndVerifyStep's per-file check is adapter.quick_compile_check(),
    whose Maven/Gradle implementations don't reach the test-compile phase
    -- for a file living under src/test/java that gives weaker signal
    than CoverageApplyAndVerifyStep's own run_specific_tests() does. Still
    safe (the checkpoint's full build/test right after still bisect-
    reverts anything genuinely broken), just less precise for this
    specific reuse than a bespoke test-aware check would be.

    Bounded the same way MaintainabilityDebtCheckStep is (an iteration
    cap, not "loop until A no matter what") -- flags whatever's left
    after the cap for manual review rather than looping forever chasing a
    rating some smell genuinely can't reach automatically."""
    name: str = "coverage_quality_gate_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s.setdefault("temp:coverage_quality_iteration", 0)
        s["temp:coverage_quality_iteration"] += 1
        maxed_out = s["temp:coverage_quality_iteration"] > 3

        if not s[sk.FILES_COMPLETED]:
            # Nothing committed this run means no branch of its own was
            # ever scanned -- nothing for this step to find or fix.
            yield Event(
                author=self.name,
                content=_msg("No files completed this run — nothing to quality-check."),
                actions=EventActions(escalate=True),
            )
            return

        branch = _scanned_branch(s)
        ratings = sonar_tools.get_quality_ratings(s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], branch)
        already_a = ratings.get("sqale_rating") == "1.0"
        if already_a or maxed_out:
            reason = "Maintainability rating is A" if already_a else "hit the re-fix iteration cap"
            yield Event(
                author=self.name,
                content=_msg(f"Quality check on this run's own file(s): {reason}."),
                actions=EventActions(escalate=True),
            )
            return

        all_issues = sonar_tools.fetch_issues_and_hotspots(s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], branch)
        my_files = set(s[sk.FILES_COMPLETED])
        already_excluded = set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) | {f["file"] for f in s[sk.FILES_FLAGGED]}
        candidates = [
            i for i in all_issues
            if i["category"] == "MAINTAINABILITY" and i["component_path"] in my_files
            and i["component_path"] not in already_excluded
        ]

        if not candidates:
            yield Event(
                author=self.name,
                content=_msg("No new code smells found in this run's own file(s)."),
                actions=EventActions(escalate=True),
            )
            return

        # rule_description is needed for the fix prompt -- see
        # techdebt_agent's FetchPrioritizeStep for the identical caching
        # reasoning (many issues share the same rule).
        cache = s.setdefault("temp:coverage_rule_description_cache", {})
        for issue in candidates:
            rule_key = issue["rule_key"]
            if rule_key not in cache:
                cache[rule_key] = sonar_tools.get_rule_description(s["sonar_base_url"], rule_key, s["sonar_token"])
            issue["rule_description"] = cache[rule_key]

        groups: dict[str, list[dict]] = {}
        for i in candidates:
            groups.setdefault(i["component_path"], []).append(i)
        s[sk.ORDERED_FILES_REMAINING] = [{"file": path, "issues": issues} for path, issues in groups.items()]
        yield Event(author=self.name, content=_msg(
            f"Found {len(candidates)} new code smell(s) introduced in {len(groups)} file(s) this run wrote — re-fixing."
        ))


def _build_coverage_quality_loop() -> LoopAgent:
    return LoopAgent(
        name="coverage_quality_loop",
        sub_agents=[CoverageQualityGateStep(), PerFileLoopStep(loop=_build_techdebt_per_file_loop())],
        max_iterations=4,
    )


coverage_quality_loop = _build_coverage_quality_loop()


class CoverageReportStep(BaseAgent):
    name: str = "coverage_report_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        duration = time.time() - s.get(sk.RUN_START_TIME, time.time())
        # dict.fromkeys, not set(): a file can legitimately end up in
        # FILES_COMPLETED twice -- once from coverage's own pass, again
        # from coverage_quality_loop's reuse of techdebt_agent's
        # ApplyAndVerifyStep (which unconditionally appends on success,
        # not knowing this file was already "done" for a different
        # reason) -- de-duped here for display, order preserved.
        files = list(dict.fromkeys(s.get(sk.FILES_COMPLETED, [])))
        issues = s.get(sk.ISSUES_FIXED, [])
        flagged = s.get(sk.FILES_FLAGGED, [])
        tokens = s.get(sk.TOKEN_USAGE, {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0})

        coverage_value = get_metric_value(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], "coverage", _scanned_branch(s)
        )
        coverage_before = s.get("temp:coverage_before")
        if coverage_value is None:
            coverage_line = "unknown (no analysis yet)"
        elif coverage_before is None:
            coverage_line = f"{float(coverage_value):.1f}% (baseline unknown)"
        else:
            delta = float(coverage_value) - coverage_before
            arrow = "+" if delta >= 0 else ""
            coverage_line = f"{coverage_before:.1f}% → {float(coverage_value):.1f}% ({arrow}{delta:.1f} pts)"

        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], _scanned_branch(s)
        )
        maintainability_line = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}.get(
            ratings.get("sqale_rating"), "unknown"
        )

        lines = [
            f"**Sonar Coverage-Enhance complete** — branch `{s.get(sk.BRANCH_NAME, 'unknown')}`",
            "",
            f"- Files with new/updated tests: {len(files)}"
            + (f": {', '.join(f'`{f}`' for f in files)}" if files else ""),
            f"- Coverage-related test additions: {len(issues)}",
        ]
        if flagged:
            lines.append(f"- Flagged for manual review ({len(flagged)}):")
            for entry in flagged:
                lines.append(f"  - `{entry['file']}` — {entry['reason']}")
        push_result = s.get("temp:push_result", "not attempted")
        lines.append(f"- Push: {push_result}")
        lines.append(
            f"- Duration: {_format_duration(duration)}, tokens consumed: {tokens['total_tokens']} "
            f"(prompt: {tokens['prompt_tokens']}, output: {tokens['candidates_tokens']})"
        )
        lines.append(f"- Project coverage: {coverage_line}")
        lines.append(f"- Maintainability rating on this branch: {maintainability_line}")

        yield Event(author=self.name, content=_msg("\n".join(lines)))


coverage_pipeline = SequentialAgent(
    name="sonar_coverage_pipeline",
    sub_agents=[
        SetupStep(), CoverageBaselineStep(), coverage_outer_loop, coverage_quality_loop,
        PushStep(), CoverageReportStep(),
    ],
)

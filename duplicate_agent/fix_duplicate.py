"""Phase V — Fix Duplication Agent pipeline.

Reuses core's tool-agnostic fix-loop engine -- see
sonar/coverage_agent/enhance_coverage.py's module docstring for the full
reasoning on what's reused as-is (PushStep, OuterExitCheck, PerFileLoopStep,
FixLlmGateStep, the LLM call plumbing, and the shared sonar/setup.py +
sonar/checkpoint.py) versus what's genuinely domain-specific below.

Unlike coverage, a duplication fix IS a diff against an existing file — the
same shape as an autofix fix — so this reuses patch_tools.apply_diff
directly rather than the full-file-rewrite approach coverage needs for a
possibly-brand-new test file.

The previous version of this file (DuplicateFixerStep) was a placeholder:
no LLM call, `time.sleep(2.0)` standing in for real work, only ever the
single most-duplicated file per run, and a report step with hardcoded fake
numbers. This replaces it with a real per-file loop: every duplicated file
gets a genuine LLM-generated refactor, a real compile-check verification,
and the same checkpoint-gated full build/re-scan safety net every autofix
file goes through."""

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
    _hide_text, _llm_error_message, _looks_like_diff, _no_safe_fix_reason,
)
from core.agents.outer_loop import OuterExitCheck
from core.agents.report import PushStep, _format_duration
from core.tools import git_tools
from core.tools.patch_tools import apply_diff

from techdebt_agent.fix import _build_per_file_loop as _build_techdebt_per_file_loop
from techdebt_agent.maintainability import _scanned_branch
from sonar.checkpoint import build_checkpoint_gate
from sonar.setup import SetupStep
from sonar.tools import sonar_tools
from sonar.tools.sonar_tools import fetch_duplicated_files, get_metric_value
from .prompts import build_duplicate_prompt


def _project_uses_lombok(working_dir: str) -> bool:
    """Checks the project's own build file for an existing Lombok
    dependency. Lombok-annotation adoption (@Getter/@Setter/@Data/etc.) is
    the standard, safe fix for cross-file POJO/DTO/JPA-entity boilerplate
    duplication -- fields, getters, setters, equals/hashCode/toString
    repeated across structurally similar classes, not within one file,
    which a same-file helper-method extraction has nothing to grab onto.
    Confirmed live: without this, every file whose duplication was this
    shape got declined outright ("cannot be safely collapsed... without
    modifying other files"), even though Lombok elimination fixes it
    single-file. Only offered when Lombok is ALREADY a dependency --
    adding a new build dependency is outside what a single-file diff can
    safely do."""
    for build_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
        path = os.path.join(working_dir, build_file)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            if "lombok" in f.read().lower():
                return True
    return False


class DuplicateBaselineStep(BaseAgent):
    """Captures the project's duplicated-lines density before this run
    touches anything, so the final report can show a real before/after
    delta instead of a lone final number with nothing to compare it to.
    branch=source_branch (or the project default) -- this run's own
    branch doesn't exist yet."""
    name: str = "duplicate_baseline_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        value = get_metric_value(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"],
            "duplicated_lines_density", s.get("source_branch"),
        )
        s["temp:density_before"] = float(value) if value is not None else None
        note = f"{s['temp:density_before']:.1f}%" if s["temp:density_before"] is not None else "unknown"
        yield Event(author=self.name, content=_msg(f"Baseline duplication density before this run: {note}."))


class DuplicateFetchStep(BaseAgent):
    """Duplication's equivalent of outer_loop.FetchPrioritizeStep — see its
    docstring, and CoverageFetchStep's, for the exclusion-set reasoning
    (a file already completed/flagged/reverted this run is out of scope,
    not just "not yet done")."""

    name: str = "duplicate_fetch_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # s.get("source_branch") if this run targeted a specific branch,
        # else None (the project's default) -- see FetchPrioritizeStep's
        # identical reasoning in techdebt_agent/outer_loop.py.
        duplicated = fetch_duplicated_files(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], s.get("source_branch")
        )

        excluded = set(s[sk.FILES_COMPLETED]) | set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) \
            | {f["file"] for f in s[sk.FILES_FLAGGED]}
        remaining = [f for f in duplicated if f["file"] not in excluded]

        s[sk.ORDERED_FILES_REMAINING] = remaining
        yield Event(author=self.name, content=_msg(
            f"Found {len(duplicated)} file(s) with duplicated code — "
            f"{len(remaining)} queued to refactor."
        ))


class DuplicateFileFixerStep(BaseAgent):
    """Duplication's equivalent of fix.FileFixerStep."""

    name: str = "duplicate_file_fixer_step"

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

        s[sk.CURRENT_FILE_GROUP] = {"file": entry["file"], "entry": entry}
        s[sk.CURRENT_FILE_CONTENT] = file_content
        s["temp:fix_prompt"] = build_duplicate_prompt(
            file_path=entry["file"],
            file_content=file_content,
            density=entry["duplicated_lines_density"],
            blocks=entry["duplicated_blocks"],
            lombok_available=_project_uses_lombok(working_dir),
        )
        yield Event(author=self.name, content=_msg(
            f"Refactoring `{entry['file']}` ({entry['duplicated_lines_density']:.1f}% duplicated, "
            f"{entry['duplicated_blocks']} block(s))."
        ))


class DuplicateApplyAndVerifyStep(BaseAgent):
    """Duplication's equivalent of fix.ApplyAndVerifyStep — diff-apply +
    compile-check verification, with one full-file regeneration retry if
    the diff fails to apply or doesn't compile (same fallback reasoning as
    fix.ApplyAndVerifyStep._retry_full_file: an LLM miscounting unified-diff
    hunk headers is the dominant failure mode, and asking for the whole file
    sidesteps hunk arithmetic entirely). No per-issue narrow retry — unlike
    autofix, there's no set of discrete issue keys to retry narrowly against,
    just one file-level refactor."""

    name: str = "duplicate_apply_and_verify_step"

    async def _retry_full_file(self, ctx: InvocationContext, entry: dict, working_dir: str, reason: str) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s["temp:full_file_retry_ok"] = False
        s["temp:fix_prompt"] = build_duplicate_prompt(
            file_path=entry["file"],
            file_content=s[sk.CURRENT_FILE_CONTENT],
            density=entry["duplicated_lines_density"],
            blocks=entry["duplicated_blocks"],
            lombok_available=_project_uses_lombok(working_dir),
            output_format=(
                f"The previous diff-based attempt failed because {reason}. This time, "
                "output the COMPLETE corrected file — every line from start to end, "
                "with the refactor applied — not a diff. Wrap it in a single fenced "
                "code block and nothing else."
            ),
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
                f"Full-file retry for `{entry['file']}` failed: the model call failed ({llm_call_error})."
            ))
            return

        raw = s.get(sk.PROPOSED_DIFF, "")
        no_safe_fix_reason = _no_safe_fix_reason(raw)
        if no_safe_fix_reason is not None:
            s["temp:no_safe_fix_reason"] = no_safe_fix_reason
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{entry['file']}` declined: {no_safe_fix_reason}"
            ))
            return

        content = _extract_code_block(raw).strip()
        if not content or _looks_like_diff(content):
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{entry['file']}` still returned diff-shaped or empty output — "
                "declining rather than writing it."
            ))
            return

        with open(os.path.join(working_dir, entry["file"]), "w", encoding="utf-8") as f:
            f.write(content)
        s["temp:full_file_retry_ok"] = True
        yield Event(author=self.name, content=_msg(f"Full-file refactor for `{entry['file']}` generated — verifying."))

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        group = s[sk.CURRENT_FILE_GROUP]
        entry = group["entry"]
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)

        def _flag_and_skip(reason: str) -> None:
            s[sk.FILES_FLAGGED].append({"file": entry["file"], "reason": reason})
            s[sk.ORDERED_FILES_REMAINING].pop(0)

        llm_call_error = s.pop("temp:llm_call_error", None)
        if llm_call_error is not None:
            _flag_and_skip(f"model call failed ({llm_call_error}) — no refactor was generated")
            yield Event(author=self.name, content=_msg(
                f"Duplication fix for `{entry['file']}` skipped: the model call failed "
                f"({llm_call_error}) — flagged for manual review."
            ))
            return

        no_safe_fix_reason = _no_safe_fix_reason(s[sk.PROPOSED_DIFF])
        if no_safe_fix_reason is not None:
            _flag_and_skip(no_safe_fix_reason)
            yield Event(author=self.name, content=_msg(
                f"Duplication fix for `{entry['file']}` declined: {no_safe_fix_reason} — flagged for manual review."
            ))
            return

        applied = apply_diff(s[sk.PROPOSED_DIFF], working_dir, entry["file"])
        retried = False
        if not applied:
            yield Event(author=self.name, content=_msg(
                f"Diff for `{entry['file']}` failed to apply — retrying with a full-file refactor."
            ))
            async for event in self._retry_full_file(ctx, entry, working_dir, "the diff failed to apply"):
                yield event
            retried = True
            applied = s.get("temp:full_file_retry_ok", False)
            if not applied:
                _flag_and_skip(s.pop("temp:no_safe_fix_reason", None) or "diff failed to apply (full-file retry also failed)")
                yield Event(author=self.name, content=_msg(
                    f"Could not apply the refactor to `{entry['file']}` even after a full-file retry — "
                    "flagged for manual review."
                ))
                return

        result = adapter.quick_compile_check(working_dir, scope=entry["file"])
        if not result.passed and not retried:
            git_tools.revert_file(working_dir, entry["file"])
            yield Event(author=self.name, content=_msg(
                f"Refactor for `{entry['file']}` applied but failed to compile — retrying with a full-file refactor."
            ))
            async for event in self._retry_full_file(ctx, entry, working_dir, "the applied fix failed to compile"):
                yield event
            retried = True
            if s.get("temp:full_file_retry_ok", False):
                result = adapter.quick_compile_check(working_dir, scope=entry["file"])

        if not result.passed:
            git_tools.revert_file(working_dir, entry["file"])
            no_safe_fix_reason = s.pop("temp:no_safe_fix_reason", None)
            _flag_and_skip(no_safe_fix_reason or result.errors[-800:])
            yield Event(author=self.name, content=_msg(
                f"Refactor for `{entry['file']}`{' still' if retried else ''} failed to compile — "
                "reverted, flagged for manual review."
            ))
            return

        # Real behavioral verification (does the refactor actually preserve
        # behavior, not just compile) happens at the checkpoint's full
        # verify_build right after this step, same as every autofix file —
        # a compile-only pass here would let a subtly-wrong extraction (e.g.
        # a parameter bound to the wrong variable at one call site) through
        # undetected until then.
        commit_sha = git_tools.commit(working_dir, f"refactor: remove duplication in {entry['file']}")
        s[sk.FILES_COMPLETED].append(entry["file"])
        s[sk.ISSUES_FIXED].append(f"duplication:{entry['file']}")
        if commit_sha is not None:
            s.setdefault("temp:checkpoint_batch", []).append({
                "file": entry["file"], "commit_sha": commit_sha, "issue_keys": [f"duplication:{entry['file']}"],
            })
        s[sk.ORDERED_FILES_REMAINING].pop(0)
        s[sk.FILES_SINCE_CHECKPOINT] += 1
        yield Event(author=self.name, content=_msg(
            f"Refactored `{entry['file']}` (was {entry['duplicated_lines_density']:.1f}% duplicated, "
            f"{entry['duplicated_blocks']} block(s)) — compiled clean."
        ))


def _build_duplicate_per_file_loop() -> LoopAgent:
    return LoopAgent(
        name="duplicate_per_file_loop",
        sub_agents=[
            DuplicateFileFixerStep(),
            FixLlmGateStep(llm_agent=_build_fix_llm_agent()),
            DuplicateApplyAndVerifyStep(),
            build_checkpoint_gate(),
        ],
        max_iterations=1000,  # real exit is DuplicateFileFixerStep's escalate=True on empty queue
    )


duplicate_outer_loop = LoopAgent(
    name="duplicate_outer_loop",
    sub_agents=[
        DuplicateFetchStep(),
        PerFileLoopStep(loop=_build_duplicate_per_file_loop()),
        OuterExitCheck(),
    ],
    max_iterations=5,
)


class DuplicateQualityGateStep(BaseAgent):
    """Post-pass check, after duplicate_outer_loop (and the checkpoint that
    fires when its queue empties has already re-scanned this run's own
    branch): look for new MAINTAINABILITY code smells Sonar found
    specifically in the file(s) THIS run refactored -- an extraction can
    introduce its own smell (an unused import left behind, a helper method
    that itself reads as duplicated, a magic number pulled out without a
    name) even while genuinely reducing duplication. Scoped to files in
    FILES_COMPLETED only -- pre-existing production-code debt elsewhere in
    the project is techdebt_agent's job, not this agent's.

    Reuses techdebt_agent's own per-file loop (FileFixerStep/
    ApplyAndVerifyStep/build_fix_prompt via _build_techdebt_per_file_loop)
    to actually fix what's found -- see CoverageQualityGateStep's identical
    reasoning and its documented quick_compile_check-on-a-non-main-file
    caveat, which applies here too whenever the refactored file happens to
    live under src/test/java rather than src/main/java.

    Bounded the same way MaintainabilityDebtCheckStep is (an iteration
    cap, not "loop until A no matter what") -- flags whatever's left after
    the cap for manual review rather than looping forever."""
    name: str = "duplicate_quality_gate_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s.setdefault("temp:duplicate_quality_iteration", 0)
        s["temp:duplicate_quality_iteration"] += 1
        maxed_out = s["temp:duplicate_quality_iteration"] > 3

        if not s[sk.FILES_COMPLETED]:
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

        cache = s.setdefault("temp:duplicate_rule_description_cache", {})
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
            f"Found {len(candidates)} new code smell(s) introduced in {len(groups)} file(s) this run refactored — re-fixing."
        ))


def _build_duplicate_quality_loop() -> LoopAgent:
    return LoopAgent(
        name="duplicate_quality_loop",
        sub_agents=[DuplicateQualityGateStep(), PerFileLoopStep(loop=_build_techdebt_per_file_loop())],
        max_iterations=4,
    )


duplicate_quality_loop = _build_duplicate_quality_loop()


class DuplicateReportStep(BaseAgent):
    name: str = "duplicate_report_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        duration = time.time() - s.get(sk.RUN_START_TIME, time.time())
        files = s.get(sk.FILES_COMPLETED, [])
        issues = s.get(sk.ISSUES_FIXED, [])
        flagged = s.get(sk.FILES_FLAGGED, [])
        tokens = s.get(sk.TOKEN_USAGE, {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0})

        density_value = get_metric_value(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"],
            "duplicated_lines_density", _scanned_branch(s),
        )
        density_before = s.get("temp:density_before")
        if density_value is None:
            density_line = "unknown (no analysis yet)"
        elif density_before is None:
            density_line = f"{float(density_value):.1f}% (baseline unknown)"
        else:
            delta = float(density_value) - density_before
            arrow = "+" if delta >= 0 else ""
            density_line = f"{density_before:.1f}% → {float(density_value):.1f}% ({arrow}{delta:.1f} pts)"

        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], _scanned_branch(s)
        )
        maintainability_line = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}.get(
            ratings.get("sqale_rating"), "unknown"
        )

        # dict.fromkeys, not set(): a file can legitimately end up in
        # FILES_COMPLETED twice -- once from this run's own refactor pass,
        # again from duplicate_quality_loop's reuse of techdebt_agent's
        # ApplyAndVerifyStep (which unconditionally appends on success,
        # not knowing this file was already "done" for a different
        # reason) -- de-duped here for display, order preserved.
        files = list(dict.fromkeys(files))

        lines = [
            f"**Sonar Duplicate-Fix complete** — branch `{s.get(sk.BRANCH_NAME, 'unknown')}`",
            "",
            f"- Files refactored: {len(files)}" + (f": {', '.join(f'`{f}`' for f in files)}" if files else ""),
            f"- Duplicate blocks resolved: {len(issues)}",
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
        lines.append(f"- Project duplicated-lines density: {density_line}")
        lines.append(f"- Maintainability rating on this branch: {maintainability_line}")

        yield Event(author=self.name, content=_msg("\n".join(lines)))


duplicate_pipeline = SequentialAgent(
    name="sonar_duplicate_pipeline",
    sub_agents=[
        SetupStep(), DuplicateBaselineStep(), duplicate_outer_loop, duplicate_quality_loop,
        PushStep(), DuplicateReportStep(),
    ],
)

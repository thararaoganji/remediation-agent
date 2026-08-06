"""
ADK wiring for the sonar auto-fix workflow.

Design rule carried over from the review: LlmAgent is used ONLY for
Section 6 fix generation. Every orchestration decision — branch setup,
prioritization, cluster classification, checkpoint gating, loop exit — is a
custom BaseAgent making a deterministic decision from session.state and
tool results. This keeps Principle #2 (orchestration never varies with
language or issue count) actually true at the framework level, not just on
paper: an LlmAgent could always decide to improvise, a BaseAgent can't.

This module builds `pipeline_agent`, the deterministic Sonar remediation
graph. It is not the package's `root_agent` — `intake.py` wraps it behind a
conversational front door that gathers the repo location first; see
`sonar_autofix_agent/__init__.py`.

Composition (top to bottom):

  pipeline_agent (SequentialAgent)
  ├── setup_step           (BaseAgent)  -- Phase I
  ├── fetch_prioritize_step(BaseAgent)  -- Phase II, called once + each outer iter
  └── outer_loop           (LoopAgent, max_iterations = MAX_OUTER_ITERATIONS)
        ├── refetch_prioritize_step (BaseAgent)   -- 5.5 re-fetch
        ├── per_file_loop           (LoopAgent, iterations = queue length)
        │     ├── file_fixer_step   (BaseAgent)   -- 5.1/5.2 prep + deterministic pre-pass
        │     ├── fix_llm_gate_step (BaseAgent)   -- calls fix_llm_agent unless every issue
        │     │     └── fix_llm_agent (LlmAgent)  -- was resolved deterministically already
        │     ├── apply_and_verify_step (BaseAgent) -- apply diff, compile check, verify
        │     └── checkpoint_gate   (BaseAgent)   -- fires checkpoint_pipeline conditionally
        │           └── checkpoint_pipeline (SequentialAgent) -- Section 5.4
        └── outer_exit_check (BaseAgent) -- escalate=True when queue empty or max hit
  └── report_step           (BaseAgent)  -- Phase IV, always runs (SequentialAgent tail)
"""

import difflib
import os
import re
import tempfile
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import state_schema as sk
from .adapters.base import get_adapter, ToolNotAvailableError, BuildToolNotDetectedError
from .prompts import build_fix_prompt
from .tools import sonar_tools, patch_tools, git_tools, deterministic_fixes


def _hide_text(event: Event) -> Event:
    """Strips a text-bearing event's visible content while preserving its
    `actions` (notably `state_delta`, which is how output_key writes
    reach session.state — see LlmAgent.__maybe_save_output_to_state).
    ADK only applies an event's state_delta when that event reaches the
    top-level Runner via session_service.append_event(); a step that
    drops the event entirely (rather than yielding a version of it) also
    drops that write. Confirmed live: dropping fix_llm_agent's own
    response event this way left session.state[PROPOSED_DIFF] unset
    entirely (KeyError downstream) -- and _retry_full_file's identical,
    pre-existing pattern had the same bug the whole session, silently
    reading PROPOSED_DIFF's STALE value from the original failed diff
    attempt instead of the actual full-file regeneration, which explains
    the "corrupted diff-shaped full file" symptom diagnosed earlier as a
    model-quality issue -- it was actually just old data.
    Non-text events (function calls/responses) pass through unchanged;
    only cosmetic content is touched, `.model_copy` leaves `actions` (and
    everything else) as the same object."""
    if not (event.content and any(getattr(p, "text", None) for p in event.content.parts or [])):
        return event
    return event.model_copy(update={"content": None})


def _msg(text: str) -> types.Content:
    """Every custom BaseAgent step below was originally silent
    (content=None) — deterministic orchestration doesn't need an LLM to
    narrate it, so there's no model turn to show. But that leaves adk
    web's event list full of unlabeled placeholder entries with nothing
    to click into. This wraps a short, fixed status line as the event's
    content instead — still not LLM-generated, just a visible echo of
    the decision the step already made."""
    return types.Content(role="model", parts=[types.Part(text=text)])


_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_DIFF_ARTIFACT_RE = re.compile(r"^diff --git |^@@ .*@@", re.MULTILINE)


def _looks_like_diff(text: str) -> bool:
    """Observed live: when a full-file retry is asked to regenerate the
    whole file (see _retry_full_file), the model can still degrade back
    into diff-shaped output partway through — the extracted 'full file'
    ends with a literal `diff --git a/...` / `@@ ... @@` block instead of
    real source, which then fails to compile with confusing javac errors
    ("class, interface, enum, or record expected") pointing at that
    embedded diff syntax. Catching this before writing to disk turns a
    wasted compile-check cycle into an immediate, clearly-explained
    decline."""
    return bool(_DIFF_ARTIFACT_RE.search(text))


def _extract_code_block(text: str) -> str:
    """fix_llm_agent's responses are consistently prose explanation followed
    by one fenced code block — every response observed live follows this
    shape, even when the prompt explicitly asks for raw output only. Used by
    ApplyAndVerifyStep's full-file retry to pull just the file content back
    out. Falls back to the raw text if no fence is found, in case the model
    does comply literally."""
    m = _CODE_FENCE_RE.search(text)
    return m.group(1) if m else text


_FIX_SUMMARY_DIFF_CHAR_LIMIT = 1500


def _build_fix_summary(file_path: str, issues: list[dict], before: str, after: str) -> str:
    """The one place a fix's actual content is shown in chat/web —
    "error" (the Sonar issue(s) that triggered this fix) plus "resolution"
    (a compact diff of what changed), replacing what used to be shown
    piecemeal: fix_llm_agent's own verbose prose+diff response, a second
    diff dump from a full-file retry, and no diff at all for deterministic
    fixes. Diffed against the TRUE pre-fix content (before any
    deterministic or LLM change), so a file that got both still shows one
    combined diff, not two. Truncated — a full class-level diff dumped
    into chat for every fix is the "displays too much" complaint this
    exists to fix."""
    lines = [f"- `{i['rule_key']}`: {i['message']}" for i in issues]
    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=file_path, tofile=file_path,
    ))
    diff_text = "".join(diff_lines) or "(no textual difference from the original)"
    if len(diff_text) > _FIX_SUMMARY_DIFF_CHAR_LIMIT:
        diff_text = diff_text[:_FIX_SUMMARY_DIFF_CHAR_LIMIT] + "\n… (truncated)"
    return f"Fixed `{file_path}`:\n" + "\n".join(lines) + f"\n```diff\n{diff_text}\n```"


def _java_fqcn(file_path: str) -> str:
    """Converts a Java source path (as reported by Sonar, relative to the
    repo root) to its fully-qualified class name — e.g.
    'src/test/java/portal/expenses/controller/AuthControllerTest.java' ->
    'portal.expenses.controller.AuthControllerTest'. Assumes the standard
    Maven/Gradle layout (a 'java/' segment marking the source root)."""
    parts = file_path.replace("\\", "/").split("/")
    if "java" in parts:
        parts = parts[parts.index("java") + 1:]
    joined = "/".join(parts)
    if joined.endswith(".java"):
        joined = joined[: -len(".java")]
    return joined.replace("/", ".")


_RATING_LETTERS = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_summary(report: dict) -> str:
    """Human-readable close-out message for ReportStep. Per-file detail is
    deliberately just a comma-joined list of file names, not a per-file
    breakdown — the individual fix/checkpoint/re-scan events already
    narrated each file's outcome as it happened."""
    lines = [f"**Sonar Auto-Fix complete** — branch `{report['branch_name']}`", ""]

    lines.append(f"- Issues fixed: {len(report['issues_fixed'])}")

    files_completed = report["files_completed"]
    if files_completed:
        lines.append(f"- Files fixed ({len(files_completed)}): {', '.join(f'`{f}`' for f in files_completed)}")
    else:
        lines.append("- Files fixed: none")

    flagged = report["files_flagged_for_manual_review"]
    if flagged:
        lines.append(f"- Flagged for manual review ({len(flagged)}):")
        for entry in flagged:
            lines.append(f"  - `{entry['file']}` — {entry['reason']}")

    review_queue = report["wont_fix_review_queue"]
    if review_queue:
        lines.append(f"- Awaiting human decision (Minor/Low Security or Reliability): {len(review_queue)} issue(s)")

    lines.append(f"- Checkpoints: {len(report['checkpoints'])}, outer loop iterations: {report['outer_iterations']}"
                  + (" (hit max)" if report["hit_max_iterations"] else ""))

    push_result = report["push_result"]
    push_label = {"pushed": f"Pushed `{report['branch_name']}` to origin."}.get(
        push_result, push_result[0].upper() + push_result[1:] if push_result != "not attempted" else "Not attempted."
    )
    lines.append(f"- Push: {push_label}")

    tokens = report["tokens_consumed"]
    lines.append(
        f"- Duration: {_format_duration(report['duration_seconds'])}, "
        f"tokens consumed: {tokens['total_tokens']} "
        f"(prompt: {tokens['prompt_tokens']}, output: {tokens['candidates_tokens']})"
    )

    _metric_names = {"sqale_rating": "Maintainability", "security_rating": "Security", "reliability_rating": "Reliability"}
    ratings = ", ".join(
        f"{_metric_names.get(metric, metric)}: {_RATING_LETTERS.get(grade, grade)}"
        for metric, grade in report["final_ratings"].items()
    )
    lines.append(f"- Final quality ratings — {ratings}")

    if report["note_if_not_a"]:
        lines.append(f"\n{report['note_if_not_a']}")
    else:
        lines.append("\nAll categories are rated A.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase I — Setup (Section 3)
# ---------------------------------------------------------------------------

class SetupStep(BaseAgent):
    name: str = "setup_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = git_tools.resolve_source(
            s["source"],
            s[sk.SOURCE_TYPE],
            workspace_root=s.get(
                "workspace_root", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
            ),
            github_token=s.get("github_token"),
        )
        # Fail fast, before any Sonar fetch or LLM call: resolve the actual
        # build tool (auto-detected for a generic "java" LANGUAGE, or the
        # explicit override from .env) and confirm the required binaries
        # are on PATH. ToolNotAvailableError / BuildToolNotDetectedError are
        # deliberately NOT caught here — they propagate out of SetupStep and
        # stop the whole run immediately, with a clear actionable message,
        # rather than failing confusingly deep inside the per-file loop on
        # the first quick_compile_check().
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        adapter.preflight_check(working_dir)
        s["temp:resolved_language"] = type(adapter).__name__

        # Read from the build file, not .env: the project key the Sonar
        # plugin actually uses when run_sonar_scan() invokes `gradle sonar`
        # / `mvn sonar:sonar` is whatever's configured in build.gradle/
        # pom.xml — a mismatched .env value would fetch/report against one
        # project key while the scan itself analyzes under another.
        s[sk.SONAR_PROJECT_KEY] = adapter.get_project_key(working_dir)

        # SonarPreflightError deliberately NOT caught here — same "fail
        # fast before any branch is created or issue fetched" contract as
        # the tool/build-file checks above. A project key that resolves
        # cleanly from the build file can still be one that's never been
        # scanned on this server (or was scanned under a different key) —
        # without this, the run would proceed to create a branch and then
        # silently find 0 issues, with nothing telling the user why.
        sonar_tools.validate_connection(s["sonar_base_url"], s["sonar_token"])
        sonar_tools.check_project_analyzed(s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"])

        branch_name, resumed = git_tools.find_or_create_branch(
            working_dir, s[sk.SONAR_PROJECT_KEY], s.get("timestamp", "")
        )
        s[sk.WORKING_DIR] = working_dir
        s[sk.BRANCH_NAME] = branch_name

        s.setdefault(sk.OUTER_ITERATION, 0)
        s.setdefault(sk.MAX_OUTER_ITERATIONS, 5)
        s.setdefault(sk.CHECKPOINT_BATCH_SIZE, 8)
        s.setdefault(sk.FILES_SINCE_CHECKPOINT, 0)
        s.setdefault(sk.FILES_COMPLETED, [])
        s.setdefault(sk.FILES_FLAGGED, [])
        s.setdefault(sk.ISSUES_FIXED, [])
        s.setdefault(sk.ISSUES_NO_SAFE_FIX, [])
        s.setdefault(sk.CHECKPOINTS, [])
        s.setdefault(sk.WONT_FIX_REVIEW_QUEUE, [])
        s.setdefault(sk.MAINTAINABILITY_EXPANSION_ITERATION, 0)
        s.setdefault(sk.MAINTAINABILITY_EXPANSION_BATCH_SIZE, 8)
        # Resuming a run: ADK's SessionService is relied on to have already
        # restored the above from persisted state — but InMemorySessionService
        # has no cross-invocation persistence, so a genuinely new session
        # resuming a branch with real commits already on it (from an
        # earlier, separate invocation against the same local repo) would
        # otherwise start FILES_COMPLETED empty and silently re-fix every
        # already-fixed file from scratch (observed live — a redundant
        # re-fix that happened to produce a no-op diff crashed the whole
        # run in git_tools.commit()). Only reconstructs when state is
        # actually empty, so a real same-session resume is untouched.
        if resumed and not s[sk.FILES_COMPLETED]:
            s[sk.FILES_COMPLETED] = git_tools.completed_files_from_history(working_dir)
        verb = "Resumed" if resumed else "Checked out"
        yield Event(author=self.name, content=_msg(f"{verb} branch `{branch_name}`. Fetching Sonar issues next."))


# ---------------------------------------------------------------------------
# Phase II — Fetch & Prioritize (Section 4)
# ---------------------------------------------------------------------------

class FetchPrioritizeStep(BaseAgent):
    name: str = "fetch_prioritize_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # branch=None (the project's default branch, e.g. main) deliberately
        # — not s[sk.BRANCH_NAME]. The agent's own {project_key}_agent_*
        # branch has no Sonar analysis of its own until checkpoint_pipeline
        # scans it for the first time, so querying it by name here 404s.
        # main's already-scanned issue list is the actual "what needs
        # fixing" source of truth; FILES_COMPLETED/FILES_FLAGGED (updated
        # locally as files are fixed/reverted) is what keeps re-fetching
        # the same static main list from reprocessing already-handled
        # files across outer_loop iterations.
        issues = sonar_tools.fetch_issues_and_hotspots(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], None
        )
        # Cached for the debt-ratio expansion pass later so it doesn't need
        # a second full fetch just to find Minor Maintainability candidates.
        s["temp:all_fetched_issues"] = issues

        ordered, review_queue = sonar_tools.partition_and_prioritize(issues)
        # dedupe against anything already surfaced in a resumed run
        existing_review_keys = {i["issue_key"] for i in s[sk.WONT_FIX_REVIEW_QUEUE]}
        s[sk.WONT_FIX_REVIEW_QUEUE].extend(
            i for i in review_queue if i["issue_key"] not in existing_review_keys
        )

        completed = set(s[sk.FILES_COMPLETED])
        remaining = [g for g in ordered if g["file"] not in completed]

        # build_fix_prompt() needs rule_description per issue (Section 6);
        # fetched lazily here, scoped to only the in-scope autofix issues
        # actually about to be prompted, and cached by rule_key for the
        # rest of this run (many issues share the same rule) — not fetched
        # for the whole raw response, which would include out-of-scope and
        # review-lane issues that never reach the LLM.
        rule_desc_cache: dict = s.setdefault("temp:rule_description_cache", {})
        for group in remaining:
            for issue in group["issues"]:
                rule_key = issue["rule_key"]
                if rule_key not in rule_desc_cache:
                    rule_desc_cache[rule_key] = sonar_tools.get_rule_description(
                        s["sonar_base_url"], rule_key, s["sonar_token"]
                    )
                issue["rule_description"] = rule_desc_cache[rule_key]

        s[sk.ORDERED_FILES_REMAINING] = remaining
        yield Event(author=self.name, content=_msg(
            f"Fetched {len(issues)} Sonar issue(s)/hotspot(s) — "
            f"{len(remaining)} file(s) queued to fix, "
            f"{len(s[sk.WONT_FIX_REVIEW_QUEUE])} total in the manual-review queue."
        ))


# ---------------------------------------------------------------------------
# Per-file loop body (Section 5.1-5.3)
# ---------------------------------------------------------------------------

class FileFixerStep(BaseAgent):
    """Pops the next file, classifies clusters (5.2/6.1 resolution: colliding
    excluded here, independent+nested flattened for a single prompt), and
    writes the prompt into temp: state for fix_llm_agent to consume."""
    name: str = "file_fixer_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        queue = s[sk.ORDERED_FILES_REMAINING]
        if not queue:
            s[sk.FILE_LOOP_DONE] = True
            yield Event(
                author=self.name,
                content=_msg("No files left in the queue."),
                actions=EventActions(escalate=True),
            )
            return

        group = queue[0]  # keep at index 0 until fully handled; pop on success
        cluster_result = patch_tools.classify_and_prepare_batch(group["issues"])
        for issue in cluster_result.colliding_flagged:
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": "colliding textRange"})

        batch_issues = patch_tools.issues_for_prompt(cluster_result)
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])
        # group["file"] is always forward-slash-separated (Sonar's own
        # component-path convention), regardless of host OS — split and
        # rejoin with os.path.join rather than raw string interpolation so
        # the actual filesystem call always uses the native separator.
        file_abs_path = os.path.join(s[sk.WORKING_DIR], *group["file"].split("/"))
        with open(file_abs_path) as f:
            original_content = f.read()

        # Deterministic pre-pass: a handful of rules (see
        # tools/deterministic_fixes.py) have exactly one unambiguous
        # correct fix — .collect(toList()) -> .toList(), deleting
        # commented-out code, etc. Applying those with plain text surgery
        # before the LLM ever sees the file cuts cost/latency for that
        # slice and removes any chance of the LLM touching something else
        # in the same pass. Issues a fixer declines (wrong shape) fall
        # straight through to remaining_issues unchanged.
        file_content, mechanical_fixed, remaining_issues = deterministic_fixes.apply_deterministic_fixes(
            original_content, batch_issues,
        )
        if mechanical_fixed:
            with open(file_abs_path, "w") as f:
                f.write(file_content)
            rule_list = ", ".join(sorted({i["rule_key"] for i in mechanical_fixed}))
            yield Event(author=self.name, content=_msg(
                f"Deterministically fixed {len(mechanical_fixed)} issue(s) in `{group['file']}` "
                f"({rule_list}) — no LLM call needed for these."
            ))

        # CURRENT_FILE_GROUP["issues"] stays the FULL batch (mechanical +
        # LLM-bound) — ApplyAndVerifyStep uses it for ISSUES_FIXED
        # tracking, the commit's issue_keys, and (usefully) re-verifies
        # the deterministic fixes too via the same before/after count
        # check it already runs for LLM fixes.
        s[sk.CURRENT_FILE_GROUP] = {"file": group["file"], "issues": batch_issues}
        # The TRUE pre-patch text, not the post-mechanical-fix content —
        # verify_issue_patterns_resolved()'s before/after comparison and
        # _retry_full_file()'s diff both need the real starting point.
        s[sk.CURRENT_FILE_CONTENT] = original_content

        if not remaining_issues:
            # Every issue in this file was resolved by the deterministic
            # pre-pass — nothing left for fix_llm_agent to do. The file on
            # disk already IS the fix; ApplyAndVerifyStep's apply_diff
            # call is skipped for this file (see temp:skip_llm_fix) so it
            # runs its normal compile-check/verify path against what's
            # already there instead.
            s["temp:skip_llm_fix"] = True
            s[sk.PROPOSED_DIFF] = ""
            yield Event(author=self.name, content=_msg(
                f"All issue(s) in `{group['file']}` resolved deterministically."
            ))
            return

        s["temp:skip_llm_fix"] = False
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=file_content,
            issues_bottom_to_top=remaining_issues,
            language_addendum=adapter.get_fix_prompt_addendum(),
        )
        yield Event(author=self.name, content=_msg(
            f"Fixing `{group['file']}` ({len(remaining_issues)} issue(s)"
            f"{f', {len(mechanical_fixed)} more fixed deterministically' if mechanical_fixed else ''})."
        ))


# Caps (doesn't disable) Gemini's thinking tokens for fix_llm_agent.
# Observed live: thinking_token_count ranged from ~90 to over 8000 across
# single-file fixes with no cap set (the default is per-model automatic,
# effectively unbounded) — often larger than the fix itself. These are
# narrow, single-file, already-scoped fixes, not open-ended reasoning
# tasks, so a cap trims the worst outliers without well-reasoned fixes
# needing that much budget in the first place. -1 (automatic, uncapped)
# restores the pre-cap behavior if quality regresses in practice.
_FIX_LLM_THINKING_BUDGET = int(os.environ.get("FIX_LLM_THINKING_BUDGET", "4096"))


def _build_fix_llm_agent() -> LlmAgent:
    """The only LLM call in the entire graph. A factory, not a module-level
    singleton, because per_file_loop (which embeds this) is instantiated
    twice (outer_loop and maintainability_expansion_loop) — ADK agents are
    single-parent nodes, so each embedding needs its own instance."""
    return LlmAgent(
        name="fix_llm_agent",
        model="gemini-3.5-flash",
        instruction="{temp:fix_prompt}",  # ADK injects state directly into instruction
        output_key=sk.PROPOSED_DIFF,
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=_FIX_LLM_THINKING_BUDGET),
        ),
    )


class FixLlmGateStep(BaseAgent):
    """Wraps fix_llm_agent so a file whose issues were fully resolved by
    FileFixerStep's deterministic pre-pass skips the LLM call entirely,
    instead of asking the model to regenerate a diff for zero remaining
    issues. Manually invokes the wrapped LlmAgent's .run_async(ctx) — the
    same free-standing-agent invocation pattern CheckpointGate already
    uses for checkpoint_pipeline — so its output_key write and
    usage_metadata plumbing behave exactly as when it was a bare
    per_file_loop sub_agent."""
    name: str = "fix_llm_gate_step"
    llm_agent: LlmAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s.get("temp:skip_llm_fix"):
            return
        # Hide the model's own prose+diff text — it's verbose (explanation
        # of the fix plus the full unified diff) and gets replaced by
        # ApplyAndVerifyStep's own concise "issue + compact diff" summary
        # once the fix is actually confirmed to work. Still yields the
        # event (via _hide_text, content stripped but actions/state_delta
        # intact) rather than dropping it outright — dropping it would
        # also drop the output_key write that lands PROPOSED_DIFF in
        # session.state; see _hide_text's docstring.
        async for event in self.llm_agent.run_async(ctx):
            yield _hide_text(event)


class ApplyAndVerifyStep(BaseAgent):
    """Section 5.3 body: apply diff, quick compile check, then the
    verification (not regeneration) step from the 5.2/6.1 resolution."""
    name: str = "apply_and_verify_step"

    async def _retry_full_file(
        self, ctx: InvocationContext, group: dict, working_dir: str, reason: str,
    ) -> AsyncGenerator[Event, None]:
        """Fallback for when the diff-based fix fails — either git apply
        rejects it outright, or it applies but the result doesn't compile.
        Both were observed live to share the same root cause: fix_llm_agent
        miscounting unified-diff hunk headers on larger, multi-hunk edits.
        A wrong header either makes git apply reject the whole patch, or —
        worse — 'succeed' with some hunks silently mis-merged (e.g. a field
        rename applied but not every call site updated), which then fails
        at compile time instead of at apply time. Asking for the WHOLE file
        instead of a diff sidesteps hunk arithmetic entirely.

        Sets state["temp:full_file_retry_ok"] rather than returning a value
        (async generators can't `return` one) — the caller checks it once
        this generator is fully drained. Uses a fresh, unregistered
        LlmAgent (see _build_fix_llm_agent()'s docstring on why factories
        instead of singletons sidestep ADK's single-parent constraint) so
        this can run from inside another step with no sub_agents wiring,
        the same pattern IntakeStep/CheckpointGate already use for
        one-off/nested agent invocations."""
        s = ctx.session.state
        s["temp:full_file_retry_ok"] = False
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=s[sk.CURRENT_FILE_CONTENT],
            issues_bottom_to_top=group["issues"],
            language_addendum=adapter.get_fix_prompt_addendum(),
            output_format=(
                f"The previous diff-based attempt failed because {reason}. "
                "This time, output the COMPLETE corrected file — every line "
                "from start to end, with the fixes applied — not a diff. "
                "Wrap it in a single fenced code block and nothing else: no "
                "explanation, no per-issue breakdown, just the fenced block "
                "containing the full file."
            ),
        )
        retry_agent = _build_fix_llm_agent()
        # Hide the model's own text — that's the entire regenerated file,
        # and showing it verbatim dumps the whole class into the visible
        # chat/web log on every retry (confirmed live: a several-hundred-
        # line file rendered in full in adk web). Still yields the event
        # via _hide_text (content stripped, actions/state_delta intact),
        # not dropped outright — dropping it drops the output_key write
        # PROPOSED_DIFF depends on below, same as FixLlmGateStep.
        async for event in retry_agent.run_async(ctx):
            yield _hide_text(event)

        raw = s.get(sk.PROPOSED_DIFF, "")
        content = _extract_code_block(raw).strip()
        if not content:
            return
        if _looks_like_diff(content):
            yield Event(author=self.name, content=_msg(
                f"Full-file retry for `{group['file']}` still returned diff-shaped output "
                "instead of a complete file — declining rather than writing it, flagged for manual review."
            ))
            return
        with open(os.path.join(working_dir, group["file"]), "w") as f:
            f.write(content)
        s["temp:full_file_retry_ok"] = True
        # No diff shown here — ApplyAndVerifyStep's own summary (issue
        # list + compact diff) covers this once the fix is confirmed to
        # actually compile/verify, instead of showing it twice.
        yield Event(author=self.name, content=_msg(
            f"Full-file fix for `{group['file']}` generated — verifying."
        ))

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        group = s[sk.CURRENT_FILE_GROUP]
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])

        # A deterministic-only fix (FileFixerStep found no remaining
        # issues for the LLM) already wrote the patched content straight
        # to disk — there's no diff to apply, so treat this file as
        # already "applied" and fall through to the same compile-check/
        # verify path every other fix goes through.
        applied = True if s.get("temp:skip_llm_fix") else patch_tools.apply_diff(
            s[sk.PROPOSED_DIFF], working_dir, group["file"],
        )
        retried = False
        if not applied:
            yield Event(author=self.name, content=_msg(
                f"Diff for `{group['file']}` failed to apply — retrying with a full-file fix."
            ))
            async for event in self._retry_full_file(ctx, group, working_dir, "the diff failed to apply"):
                yield event
            retried = True
            applied = s.get("temp:full_file_retry_ok", False)
            if not applied:
                s[sk.FILES_FLAGGED].append({
                    "file": group["file"],
                    "reason": "diff failed to apply (full-file retry also failed)",
                })
                s[sk.ORDERED_FILES_REMAINING].pop(0)
                yield Event(author=self.name, content=_msg(
                    f"Could not apply the fix to `{group['file']}` even after a full-file retry — "
                    "flagged for manual review."
                ))
                return

        result = adapter.quick_compile_check(working_dir, scope=group["file"])
        if not result.passed and not retried:
            # First failure for this file, and it came from a diff that DID
            # apply — same root cause as the apply-fail branch above (a
            # miscounted hunk can merge into subtly wrong code that still
            # "applies" cleanly), so the same fallback applies here too.
            # Only one retry per file either way (the `retried` guard).
            git_tools.revert_file(working_dir, group["file"])
            yield Event(author=self.name, content=_msg(
                f"Fix for `{group['file']}` applied but failed to compile — retrying with a full-file fix."
            ))
            async for event in self._retry_full_file(ctx, group, working_dir, "the applied fix failed to compile"):
                yield event
            retried = True
            if s.get("temp:full_file_retry_ok", False):
                result = adapter.quick_compile_check(working_dir, scope=group["file"])

        if not result.passed:
            git_tools.revert_file(working_dir, group["file"])
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": result.errors})
            s[sk.ORDERED_FILES_REMAINING].pop(0)
            yield Event(author=self.name, content=_msg(
                f"Fix for `{group['file']}`{' still' if retried else ''} failed to compile — "
                "reverted, flagged for manual review."
            ))
            return

        # Re-enabling an S2187-flagged test means it's about to run for the
        # very first time — quick_compile_check above only proved it
        # compiles, not that it passes. Verify it here, in isolation, on
        # just this file, rather than letting a broken re-enabled test ride
        # into a shared checkpoint batch where the full build's failure
        # would drag every other file in that batch into a collateral
        # bisect-revert (observed live: a bad re-enabled test took 6
        # unrelated, individually-correct fixes down with it).
        if any(i["rule_key"] == "java:S2187" for i in group["issues"]):
            test_result = adapter.run_specific_tests(working_dir, [_java_fqcn(group["file"])])
            if not test_result.passed:
                git_tools.revert_file(working_dir, group["file"])
                failing = patch_tools.parse_junit_failures(working_dir)
                detail = f" (failing test(s): {'; '.join(failing)})" if failing else ""
                s[sk.FILES_FLAGGED].append({
                    "file": group["file"],
                    "reason": f"re-enabled test still fails{detail}",
                })
                s[sk.ORDERED_FILES_REMAINING].pop(0)
                yield Event(author=self.name, content=_msg(
                    f"Fix for `{group['file']}` compiled, but the re-enabled test still fails{detail} — "
                    "reverted, flagged for manual review."
                ))
                return

        # This attempt is about to succeed — clear any stale flag left by an
        # earlier outer_loop iteration's failed attempt on this same file
        # (e.g. a checkpoint revert), so the final report doesn't keep
        # showing it as needing manual review once it's actually fixed.
        s[sk.FILES_FLAGGED] = [f for f in s[sk.FILES_FLAGGED] if f["file"] != group["file"]]

        verification = patch_tools.verify_issue_patterns_resolved(
            group["file"], group["issues"], working_dir,
            original_content=s[sk.CURRENT_FILE_CONTENT],
        )
        unresolved = [k for k, ok in verification.items() if not ok]
        if unresolved:
            # Fallback path: narrow single-issue follow-up call, scoped to
            # just the unresolved issue(s) against the already-patched file.
            # (Left as a TODO hook — wire a second, narrower fix_llm_agent
            # invocation here if you hit this in practice; per the 5.2/6.1
            # resolution this should be rare, not the steady-state path.)
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": f"unresolved after patch: {unresolved}"})

        commit_sha = git_tools.commit(working_dir, f"fix: sonar issues in {group['file']}")
        s[sk.FILES_COMPLETED].append(group["file"])
        s[sk.ISSUES_FIXED].extend([i["issue_key"] for i in group["issues"]])
        if commit_sha is not None:
            # None means this was a no-op — the regenerated fix was already
            # byte-identical to what's on disk (e.g. a redundant re-fix of
            # an already-correct file). Nothing new exists for a later
            # checkpoint to revert in that case, so it's deliberately left
            # out of this checkpoint's revertible batch — including it
            # would point RunFullVerifyStep's revert at the wrong commit
            # (whatever unrelated commit HEAD already was), not at a real
            # fix for this file.
            #
            # Tracked per-checkpoint (not just FILES_COMPLETED) so
            # RunFullVerifyStep knows exactly which commits are candidates
            # to revert if the full build fails — cleared after each
            # checkpoint fires, pass or fail.
            s.setdefault("temp:checkpoint_batch", []).append({
                "file": group["file"],
                "commit_sha": commit_sha,
                "issue_keys": [i["issue_key"] for i in group["issues"]],
            })
        s[sk.ORDERED_FILES_REMAINING].pop(0)
        s[sk.FILES_SINCE_CHECKPOINT] += 1
        note = f" ({len(unresolved)} issue(s) still unresolved, also flagged)" if unresolved else ""
        with open(os.path.join(working_dir, group["file"])) as f:
            after_content = f.read()
        summary = _build_fix_summary(group["file"], group["issues"], s[sk.CURRENT_FILE_CONTENT], after_content)
        yield Event(author=self.name, content=_msg(f"{summary}{note}"))


class CheckpointGate(BaseAgent):
    """Fires the checkpoint SequentialAgent when the batch-size boundary is
    hit (Section 5.3's checkpoint_boundary_reached()). ADK's LoopAgent has
    no native 'every N iterations' primitive, so this conditional dispatch
    is implemented directly rather than forced into a workflow-agent shape."""
    name: str = "checkpoint_gate"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        if s[sk.FILES_SINCE_CHECKPOINT] >= s[sk.CHECKPOINT_BATCH_SIZE] or not s[sk.ORDERED_FILES_REMAINING]:
            async for event in checkpoint_pipeline.run_async(ctx):
                yield event
            s[sk.FILES_SINCE_CHECKPOINT] = 0
        else:
            yield Event(author=self.name, content=_msg(
                f"{s[sk.FILES_SINCE_CHECKPOINT]}/{s[sk.CHECKPOINT_BATCH_SIZE]} file(s) since last checkpoint."
            ))


# --- Checkpoint pipeline (Section 5.4) ---

class RunFullVerifyStep(BaseAgent):
    name: str = "run_full_verify_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        batch = s.get("temp:checkpoint_batch", [])
        result = adapter.verify_build(working_dir)
        reverted = []

        if not result.passed:
            # Captured before any reverts happen — this is the actual
            # compiler/test output that triggered the bisect below, and the
            # only evidence of WHY without re-deriving it after the fact
            # (as happened diagnosing a real run: the revert-list alone
            # doesn't say what broke, only what got blamed for it).
            original_error = result.errors
            # gradle -q (quiet console) never prints individual failing test
            # names/stack traces — only the summary count seen in
            # original_error — so this is the only way to name which
            # test(s) actually broke, from the JUnit XML report the Test
            # task still writes regardless of console verbosity.
            failing_tests = patch_tools.parse_junit_failures(working_dir)
            # bisect_within_checkpoint_files(): quick_compile_check() in
            # ApplyAndVerifyStep only checks the single file being patched
            # in isolation, so a full-project build regression here means
            # one of THIS checkpoint's commits broke something elsewhere
            # (e.g. a caller of a changed method signature). Not true
            # binary-search bisection — each candidate needs a full project
            # build and there's only one working tree to test against, so a
            # linear reverse-commit-order sweep (most recent first, most
            # likely culprit) is the practical tradeoff. Stops at the first
            # revert that restores a passing build.
            for entry in reversed(batch):
                git_tools.revert_commit_for_file(working_dir, entry["commit_sha"], entry["file"])
                reverted.append(entry)
                result = adapter.verify_build(working_dir)
                if result.passed:
                    break

            reverted_issue_keys = {k for entry in reverted for k in entry["issue_keys"]}
            revert_reason = "reverted: broke the full build at checkpoint"
            if failing_tests:
                revert_reason += f" (failing test(s): {'; '.join(failing_tests)})"
            for entry in reverted:
                if entry["file"] in s[sk.FILES_COMPLETED]:
                    s[sk.FILES_COMPLETED].remove(entry["file"])
                s[sk.FILES_FLAGGED].append({
                    "file": entry["file"],
                    "reason": revert_reason,
                })
            s[sk.ISSUES_FIXED] = [k for k in s[sk.ISSUES_FIXED] if k not in reverted_issue_keys]

            if not result.passed:
                # Reverted every commit in this checkpoint's batch and the
                # build is still broken — the regression predates this run
                # (or lives outside these files entirely). Not something an
                # agent should silently paper over: stop and surface it.
                raise RuntimeError(
                    f"Full build still failing after reverting all {len(batch)} file(s) "
                    f"committed since the last checkpoint ({[e['file'] for e in batch]}). "
                    "Likely a pre-existing failure, not caused by this run's fixes — "
                    f"build errors: {result.errors}"
                )

        s["temp:checkpoint_batch"] = []
        if batch and not reverted:
            msg = f"Checkpoint: full build passed ({len(batch)} file(s) since last checkpoint)."
        elif reverted:
            s["temp:checkpoint_build_error"] = {"error": original_error, "failing_tests": failing_tests}
            test_detail = f"\nFailing test(s): {'; '.join(failing_tests)}" if failing_tests else ""
            msg = (
                f"Checkpoint: build failed — reverted `{'`, `'.join(e['file'] for e in reverted)}`, then passed."
                f"{test_detail}\n"
                f"Build error that triggered the revert:\n```\n{original_error[-1500:]}\n```"
            )
        else:
            msg = "Checkpoint: full build passed."
        yield Event(author=self.name, content=_msg(msg))


class TriggerAndReconcileScanStep(BaseAgent):
    name: str = "trigger_and_reconcile_scan_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import datetime
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        checkpoint_start = datetime.datetime.utcnow().isoformat()
        task_id = sonar_tools.trigger_sonar_analysis(
            working_dir, s[sk.SONAR_PROJECT_KEY], ce_edition=s.get("ce_edition", True),
            language=s[sk.LANGUAGE], sonar_base_url=s["sonar_base_url"], sonar_token=s["sonar_token"],
        )
        sonar_tools.poll_ce_task_status(s["sonar_base_url"], s["sonar_token"], task_id, timeout_s=600)
        new_issues = sonar_tools.get_issues_created_after(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], checkpoint_start, s["sonar_token"], s[sk.BRANCH_NAME]
        )

        reverted_files = []
        if new_issues:
            # Same bisect-revert reasoning as RunFullVerifyStep: only this
            # checkpoint's own batch of commits are candidates to blame.
            # New issues attributable to a batch file get that file
            # reverted; anything else gets flagged without touching code,
            # since there's no commit here that's safe to undo for it.
            batch = s.get("temp:checkpoint_batch", [])
            batch_by_file = {entry["file"]: entry for entry in batch}
            implicated = {i["component_path"] for i in new_issues if i["component_path"] in batch_by_file}

            for file in implicated:
                entry = batch_by_file[file]
                git_tools.revert_commit_for_file(working_dir, entry["commit_sha"], entry["file"])
                reverted_files.append(entry["file"])
                if entry["file"] in s[sk.FILES_COMPLETED]:
                    s[sk.FILES_COMPLETED].remove(entry["file"])
                s[sk.ISSUES_FIXED] = [k for k in s[sk.ISSUES_FIXED] if k not in entry["issue_keys"]]
                new_count = sum(1 for i in new_issues if i["component_path"] == file)
                s[sk.FILES_FLAGGED].append({
                    "file": entry["file"],
                    "reason": f"reverted: introduced {new_count} new Sonar issue(s) found by this checkpoint's re-scan",
                })

            for i in new_issues:
                if i["component_path"] not in batch_by_file:
                    s[sk.FILES_FLAGGED].append({
                        "file": i["component_path"],
                        "reason": f"new Sonar issue after checkpoint, not attributable to this batch: "
                                  f"{i.get('rule_key')} — {i.get('message', '')}",
                    })

            if reverted_files:
                adapter = get_adapter(s[sk.LANGUAGE], working_dir)
                result = adapter.verify_build(working_dir)
                if not result.passed:
                    raise RuntimeError(
                        f"Build still failing after reverting {reverted_files} in response to new "
                        f"Sonar issues — errors: {result.errors}"
                    )

        s[sk.CHECKPOINTS].append({
            "timestamp": checkpoint_start,
            "new_issues_found": len(new_issues),
            "reverted_files": reverted_files,
            # Set by RunFullVerifyStep earlier in this same checkpoint_pipeline
            # run when a full-build failure triggered a revert — carries the
            # actual compiler/test error into the final report instead of
            # just the list of files that got blamed for it.
            "build_error": s.pop("temp:checkpoint_build_error", None),
        })
        git_tools.commit_checkpoint_marker(working_dir)
        if not new_issues:
            msg = "Re-scanned Sonar — no new issues introduced."
        elif reverted_files:
            msg = f"Re-scan found new issues — reverted `{'`, `'.join(reverted_files)}`, then re-verified clean."
        else:
            msg = f"Re-scan found {len(new_issues)} new issue(s) not attributable to this batch — flagged for review."
        yield Event(author=self.name, content=_msg(msg))


checkpoint_pipeline = SequentialAgent(
    name="checkpoint_pipeline",
    sub_agents=[RunFullVerifyStep(), TriggerAndReconcileScanStep()],
)

def _build_per_file_loop() -> LoopAgent:
    """Factory, not a module-level singleton — see _build_fix_llm_agent()."""
    return LoopAgent(
        name="per_file_loop",
        sub_agents=[
            FileFixerStep(),
            FixLlmGateStep(llm_agent=_build_fix_llm_agent()),
            ApplyAndVerifyStep(),
            CheckpointGate(),
        ],
        max_iterations=1000,  # real exit is FileFixerStep's escalate=True on empty queue
    )


def _strip_escalate(event: Event) -> Event:
    """ADK's LoopAgent re-yields every sub-agent event upward completely
    unmodified, and checks event.actions.escalate at EVERY nesting level
    it passes through. FileFixerStep's escalate=True (its own queue-empty
    signal, meant only to stop per_file_loop's iteration) therefore also
    terminates whatever LoopAgent per_file_loop is embedded in (outer_loop,
    maintainability_expansion_loop) the moment it bubbles through —
    confirmed live: outer_loop had never run a second iteration in this
    project's history, always exiting after exactly one per_file_loop
    pass regardless of whether files ended up flagged and outer_loop's
    own re-fetch-and-retry (Section 5.5, MAX_OUTER_ITERATIONS) was meant
    to give them another attempt; OuterExitCheck itself never ran (its
    OUTER_ITERATION counter stayed 0 in the final report). Stripping the
    flag here, once, at the per_file_loop boundary, means only
    per_file_loop's own LoopAgent sees it — the enclosing loop's own exit
    check (OuterExitCheck / MaintainabilityDebtCheckStep) makes its own
    independent decision instead of being silently pre-empted."""
    if not event.actions.escalate:
        return event
    return event.model_copy(update={"actions": event.actions.model_copy(update={"escalate": False})})


class PerFileLoopStep(BaseAgent):
    """Wraps a per_file_loop instance so its own internal LoopAgent exit
    signal doesn't also terminate whatever LoopAgent embeds it — see
    _strip_escalate. Manually invokes .run_async(ctx), the same
    free-standing-agent pattern used elsewhere (CheckpointGate,
    IntakeStep) for a child that needs its own event post-processing."""
    name: str = "per_file_loop_step"
    loop: LoopAgent

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        async for event in self.loop.run_async(ctx):
            yield _strip_escalate(event)


# ---------------------------------------------------------------------------
# Outer loop (Section 5.5)
# ---------------------------------------------------------------------------

class OuterExitCheck(BaseAgent):
    name: str = "outer_exit_check"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s[sk.OUTER_ITERATION] += 1
        remaining = bool(s[sk.ORDERED_FILES_REMAINING]) or bool(s[sk.FILES_FLAGGED])
        maxed_out = s[sk.OUTER_ITERATION] >= s[sk.MAX_OUTER_ITERATIONS]
        if not remaining or maxed_out:
            reason = "hit max outer iterations" if maxed_out else "file queue empty"
            yield Event(
                author=self.name,
                content=_msg(f"Outer loop done after {s[sk.OUTER_ITERATION]} iteration(s) ({reason})."),
                actions=EventActions(escalate=True),
            )
        else:
            yield Event(author=self.name, content=_msg(
                f"Outer loop iteration {s[sk.OUTER_ITERATION]} complete — re-fetching Sonar issues."
            ))


outer_loop = LoopAgent(
    name="outer_loop",
    sub_agents=[FetchPrioritizeStep(), PerFileLoopStep(loop=_build_per_file_loop()), OuterExitCheck()],
    max_iterations=5,  # hard ceiling backing MAX_OUTER_ITERATIONS in state
)


# ---------------------------------------------------------------------------
# Maintainability debt-ratio top-up (post main-pass)
# ---------------------------------------------------------------------------
# The main outer_loop above only ever targets Critical/High/Medium
# Maintainability issues, because sqale_rating is a debt-RATIO threshold
# (<=5% for A), not a worst-issue gate — most projects hit A there without
# touching every Minor code smell. This step only fires if the ratio is
# still over target after the main pass, and pulls the highest-debt-minute
# Minor/Low smells first rather than exhaustively closing the whole tail.

class MaintainabilityDebtCheckStep(BaseAgent):
    name: str = "maintainability_debt_check_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        ratio = sonar_tools.get_maintainability_debt_ratio(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], s[sk.BRANCH_NAME]
        )
        s[sk.MAINTAINABILITY_EXPANSION_ITERATION] += 1
        maxed_out = s[sk.MAINTAINABILITY_EXPANSION_ITERATION] > 3

        if ratio <= sonar_tools.MAINTAINABILITY_DEBT_RATIO_TARGET or maxed_out:
            reason = "hit expansion iteration cap" if maxed_out else "target met"
            yield Event(
                author=self.name,
                content=_msg(f"Maintainability debt ratio {ratio}% (target ≤{sonar_tools.MAINTAINABILITY_DEBT_RATIO_TARGET}%) — {reason}."),
                actions=EventActions(escalate=True),
            )
            return

        candidates = sonar_tools.debt_ratio_expansion_candidates(s.get("temp:all_fetched_issues", []))
        already_done = set(s[sk.FILES_COMPLETED])
        batch_size = s[sk.MAINTAINABILITY_EXPANSION_BATCH_SIZE]
        next_batch = [i for i in candidates if i["component_path"] not in already_done][:batch_size]

        if not next_batch:
            # Nothing left to pull but ratio's still high — genuinely needs
            # a human to look at debt outside single-issue remediation
            # (e.g. large legacy files), not more agent iterations.
            s[sk.FILES_FLAGGED].append({
                "file": "<project-wide>",
                "reason": f"sqale_debt_ratio still {ratio}% after exhausting Minor/Low candidates",
            })
            yield Event(
                author=self.name,
                content=_msg(f"Debt ratio still {ratio}% but no more Minor/Low candidates — flagged for manual review."),
                actions=EventActions(escalate=True),
            )
            return

        groups: dict[str, list[dict]] = {}
        for i in next_batch:
            groups.setdefault(i["component_path"], []).append(i)
        s[sk.ORDERED_FILES_REMAINING] = [
            {"file": path, "file_priority": (2, 2), "issues": issues}
            for path, issues in groups.items()
        ]
        yield Event(author=self.name, content=_msg(
            f"Debt ratio {ratio}% still above target — queuing {len(s[sk.ORDERED_FILES_REMAINING])} more file(s)."
        ))


maintainability_expansion_loop = LoopAgent(
    name="maintainability_expansion_loop",
    sub_agents=[MaintainabilityDebtCheckStep(), PerFileLoopStep(loop=_build_per_file_loop())],
    max_iterations=4,
)


# ---------------------------------------------------------------------------
# Phase IV — Push (Section 3 step 4 follow-through) + Report (Section 9)
# ---------------------------------------------------------------------------

class PushStep(BaseAgent):
    """Runs after outer_loop + maintainability_expansion_loop, i.e. after
    every committed file has already passed a checkpoint's full verify_build
    (CheckpointGate always fires once more when the file queue empties, so
    the last batch is never left un-checkpointed) — by construction, every
    commit on the branch at this point belongs to a build that passed."""
    name: str = "push_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        branch_name = s[sk.BRANCH_NAME]

        # No files fixed and no checkpoints run means the branch has no new
        # commits over its base (e.g. the project was already all-A) —
        # pushing an unchanged branch is just noise.
        if not s[sk.FILES_COMPLETED] and not s[sk.CHECKPOINTS]:
            s["temp:push_result"] = "skipped — no commits made this run"
            yield Event(author=self.name, content=_msg("Nothing to push — no commits made this run."))
            return

        try:
            git_tools.push_branch(working_dir, branch_name, github_token=s.get("github_token"))
        except RuntimeError as e:
            s["temp:push_result"] = f"failed — {e}"
            yield Event(author=self.name, content=_msg(
                f"Could not push `{branch_name}` to origin — {e}. Fixes are committed locally; push manually."
            ))
            return

        s["temp:push_result"] = "pushed"
        yield Event(author=self.name, content=_msg(f"Pushed branch `{branch_name}` to origin."))


class ReportStep(BaseAgent):
    name: str = "report_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # security_rating/reliability_rating/sqale_rating ONLY — duplication
        # and coverage are excluded at the tool level (IN_SCOPE_RATING_METRICS),
        # not filtered out here, so there's no way to accidentally re-include them.
        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], s[sk.BRANCH_NAME]
        )
        all_a = all(v == "1.0" for v in ratings.values())
        report = {
            "branch_name": s[sk.BRANCH_NAME],
            "issues_fixed": s[sk.ISSUES_FIXED],
            "files_completed": s[sk.FILES_COMPLETED],
            "files_flagged_for_manual_review": s[sk.FILES_FLAGGED],
            "outer_iterations": s[sk.OUTER_ITERATION],
            "hit_max_iterations": s[sk.OUTER_ITERATION] >= s[sk.MAX_OUTER_ITERATIONS],
            "checkpoints": s[sk.CHECKPOINTS],
            "final_ratings": ratings,           # e.g. {"security_rating": "1.0", ...}
            "all_categories_a": all_a,
            "wont_fix_review_queue": s[sk.WONT_FIX_REVIEW_QUEUE],  # human decides, agent never resolves these
            "push_result": s.get("temp:push_result", "not attempted"),
            # Scoped to pipeline_agent's own run (set by IntakeStep right
            # before invoking it) — excludes any time/tokens spent in the
            # conversational back-and-forth resolving which repo to use.
            "duration_seconds": time.time() - s.get(sk.RUN_START_TIME, time.time()),
            "tokens_consumed": s.get(sk.TOKEN_USAGE, {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}),
            # If False: any category still below A most likely means either
            # (a) the wont_fix_review_queue above has unresolved Minor/Low
            # Security or Reliability issues awaiting a human call, or
            # (b) Maintainability's debt ratio topped out MAINTAINABILITY_
            # EXPANSION_ITERATION's cap and got flagged in files_flagged
            # rather than looped on indefinitely.
            "note_if_not_a": None if all_a else (
                "One or more categories are below A. Check "
                "wont_fix_review_queue (Minor/Low Security or Reliability "
                "issues awaiting human resolution) and files_flagged "
                "(entries tagged '<project-wide>' indicate the "
                "Maintainability debt ratio needs attention beyond "
                "single-issue fixes)."
            ),
        }
        s["final_report"] = report
        yield Event(author=self.name, content=_msg(_format_summary(report)))


# ---------------------------------------------------------------------------
# Root agent
# ---------------------------------------------------------------------------

pipeline_agent = SequentialAgent(
    name="sonar_autofix_pipeline",
    sub_agents=[SetupStep(), outer_loop, maintainability_expansion_loop, PushStep(), ReportStep()],
)

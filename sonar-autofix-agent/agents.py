"""
ADK wiring for the sonar auto-fix workflow.

Design rule carried over from the review: LlmAgent is used ONLY for
Section 6 fix generation. Every orchestration decision — branch setup,
prioritization, cluster classification, checkpoint gating, loop exit — is a
custom BaseAgent making a deterministic decision from session.state and
tool results. This keeps Principle #2 (orchestration never varies with
language or issue count) actually true at the framework level, not just on
paper: an LlmAgent could always decide to improvise, a BaseAgent can't.

Composition (top to bottom):

  root_agent (SequentialAgent)
  ├── setup_step           (BaseAgent)  -- Phase I
  ├── fetch_prioritize_step(BaseAgent)  -- Phase II, called once + each outer iter
  └── outer_loop           (LoopAgent, max_iterations = MAX_OUTER_ITERATIONS)
        ├── refetch_prioritize_step (BaseAgent)   -- 5.5 re-fetch
        ├── per_file_loop           (LoopAgent, iterations = queue length)
        │     ├── file_fixer_step   (BaseAgent)   -- 5.1/5.2 prep + calls fix_llm_agent
        │     ├── fix_llm_agent     (LlmAgent)    -- Section 6, only LLM call in the graph
        │     ├── apply_and_verify_step (BaseAgent) -- apply diff, compile check, verify
        │     └── checkpoint_gate   (BaseAgent)   -- fires checkpoint_pipeline conditionally
        │           └── checkpoint_pipeline (SequentialAgent) -- Section 5.4
        └── outer_exit_check (BaseAgent) -- escalate=True when queue empty or max hit
  └── report_step           (BaseAgent)  -- Phase IV, always runs (SequentialAgent tail)
"""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from . import state_schema as sk
from .adapters.base import get_adapter, ToolNotAvailableError, BuildToolNotDetectedError
from .prompts import build_fix_prompt
from .tools import sonar_tools, patch_tools, git_tools


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
            workspace_root=s.get("workspace_root", "/tmp/sonar_autofix_workspaces"),
            github_token=s.get("github_token"),
        )
        branch_name, resumed = git_tools.find_or_create_branch(
            working_dir, s[sk.SONAR_PROJECT_KEY], s.get("timestamp", "")
        )
        s[sk.WORKING_DIR] = working_dir
        s[sk.BRANCH_NAME] = branch_name

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
        # Resuming a run: ADK's SessionService already restored all the
        # above from persisted state if `resumed` is True — this is the
        # idempotency requirement from the doc's Section 8, satisfied by
        # using a durable SessionService instead of a hand-rolled JSON file.
        yield Event(author=self.name, content=None)


# ---------------------------------------------------------------------------
# Phase II — Fetch & Prioritize (Section 4)
# ---------------------------------------------------------------------------

class FetchPrioritizeStep(BaseAgent):
    name: str = "fetch_prioritize_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        issues = sonar_tools.fetch_issues_and_hotspots(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"]
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
        s[sk.ORDERED_FILES_REMAINING] = [g for g in ordered if g["file"] not in completed]
        yield Event(author=self.name, content=None)


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
            yield Event(author=self.name, actions=EventActions(escalate=True))
            return

        group = queue[0]  # keep at index 0 until fully handled; pop on success
        cluster_result = patch_tools.classify_and_prepare_batch(group["issues"])
        for issue in cluster_result.colliding_flagged:
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": "colliding textRange"})

        batch_issues = patch_tools.issues_for_prompt(cluster_result)
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])
        with open(f"{s[sk.WORKING_DIR]}/{group['file']}") as f:
            file_content = f.read()

        s[sk.CURRENT_FILE_GROUP] = {"file": group["file"], "issues": batch_issues}
        s[sk.CURRENT_FILE_CONTENT] = file_content
        s["temp:fix_prompt"] = build_fix_prompt(
            file_path=group["file"],
            language=s[sk.LANGUAGE],
            file_content=file_content,
            issues_bottom_to_top=batch_issues,
            language_addendum=adapter.get_fix_prompt_addendum(),
        )
        yield Event(author=self.name, content=None)


# The only LLM call in the entire graph.
fix_llm_agent = LlmAgent(
    name="fix_llm_agent",
    model="gemini-2.5-pro",
    instruction="{temp:fix_prompt}",  # ADK injects state directly into instruction
    output_key=sk.PROPOSED_DIFF,
)


class ApplyAndVerifyStep(BaseAgent):
    """Section 5.3 body: apply diff, quick compile check, then the
    verification (not regeneration) step from the 5.2/6.1 resolution."""
    name: str = "apply_and_verify_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        group = s[sk.CURRENT_FILE_GROUP]
        working_dir = s[sk.WORKING_DIR]
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])

        applied = patch_tools.apply_diff(s[sk.PROPOSED_DIFF], working_dir, group["file"])
        if not applied:
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": "diff failed to apply"})
            s[sk.ORDERED_FILES_REMAINING].pop(0)
            yield Event(author=self.name, content=None)
            return

        result = adapter.quick_compile_check(working_dir, scope=group["file"])
        if not result.passed:
            git_tools.revert_file(working_dir, group["file"])
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": result.errors})
            s[sk.ORDERED_FILES_REMAINING].pop(0)
            yield Event(author=self.name, content=None)
            return

        verification = patch_tools.verify_issue_patterns_resolved(
            group["file"], group["issues"], working_dir
        )
        unresolved = [k for k, ok in verification.items() if not ok]
        if unresolved:
            # Fallback path: narrow single-issue follow-up call, scoped to
            # just the unresolved issue(s) against the already-patched file.
            # (Left as a TODO hook — wire a second, narrower fix_llm_agent
            # invocation here if you hit this in practice; per the 5.2/6.1
            # resolution this should be rare, not the steady-state path.)
            s[sk.FILES_FLAGGED].append({"file": group["file"], "reason": f"unresolved after patch: {unresolved}"})

        git_tools.commit(working_dir, f"fix: sonar issues in {group['file']}")
        s[sk.FILES_COMPLETED].append(group["file"])
        s[sk.ISSUES_FIXED].extend([i["issue_key"] for i in group["issues"]])
        s[sk.ORDERED_FILES_REMAINING].pop(0)
        s[sk.FILES_SINCE_CHECKPOINT] += 1
        yield Event(author=self.name, content=None)


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
            yield Event(author=self.name, content=None)


# --- Checkpoint pipeline (Section 5.4) ---

class RunFullVerifyStep(BaseAgent):
    name: str = "run_full_verify_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        adapter = get_adapter(s[sk.LANGUAGE], s[sk.WORKING_DIR])
        result = adapter.verify_build(s[sk.WORKING_DIR])
        if not result.passed:
            raise NotImplementedError("bisect_within_checkpoint_files() - revert offending file(s) only")
        yield Event(author=self.name, content=None)


class TriggerAndReconcileScanStep(BaseAgent):
    name: str = "trigger_and_reconcile_scan_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        import datetime
        s = ctx.session.state
        checkpoint_start = datetime.datetime.utcnow().isoformat()
        task_id = sonar_tools.trigger_sonar_analysis(
            s[sk.WORKING_DIR], s[sk.SONAR_PROJECT_KEY], ce_edition=s.get("ce_edition", True)
        )
        sonar_tools.poll_ce_task_status(task_id, timeout_s=600)
        new_issues = sonar_tools.get_issues_created_after(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], checkpoint_start, s["sonar_token"]
        )
        if new_issues:
            raise NotImplementedError("attempt_fix(issue) OR revert file that caused it, then re-verify + re-scan")
        s[sk.CHECKPOINTS].append({"timestamp": checkpoint_start, "new_issues_found": 0})
        git_tools.commit_checkpoint_marker(s[sk.WORKING_DIR])
        yield Event(author=self.name, content=None)


checkpoint_pipeline = SequentialAgent(
    name="checkpoint_pipeline",
    sub_agents=[RunFullVerifyStep(), TriggerAndReconcileScanStep()],
)

per_file_loop = LoopAgent(
    name="per_file_loop",
    sub_agents=[FileFixerStep(), fix_llm_agent, ApplyAndVerifyStep(), CheckpointGate()],
    max_iterations=1000,  # real exit is FileFixerStep's escalate=True on empty queue
)


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
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            yield Event(author=self.name, content=None)


outer_loop = LoopAgent(
    name="outer_loop",
    sub_agents=[FetchPrioritizeStep(), per_file_loop, OuterExitCheck()],
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
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"]
        )
        s[sk.MAINTAINABILITY_EXPANSION_ITERATION] += 1
        maxed_out = s[sk.MAINTAINABILITY_EXPANSION_ITERATION] > 3

        if ratio <= sonar_tools.MAINTAINABILITY_DEBT_RATIO_TARGET or maxed_out:
            yield Event(author=self.name, actions=EventActions(escalate=True))
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
            yield Event(author=self.name, actions=EventActions(escalate=True))
            return

        groups: dict[str, list[dict]] = {}
        for i in next_batch:
            groups.setdefault(i["component_path"], []).append(i)
        s[sk.ORDERED_FILES_REMAINING] = [
            {"file": path, "file_priority": (2, 2), "issues": issues}
            for path, issues in groups.items()
        ]
        yield Event(author=self.name, content=None)


maintainability_expansion_loop = LoopAgent(
    name="maintainability_expansion_loop",
    sub_agents=[MaintainabilityDebtCheckStep(), per_file_loop],
    max_iterations=4,
)


# ---------------------------------------------------------------------------
# Phase IV — Report (Section 9)
# ---------------------------------------------------------------------------

class ReportStep(BaseAgent):
    name: str = "report_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # security_rating/reliability_rating/sqale_rating ONLY — duplication
        # and coverage are excluded at the tool level (IN_SCOPE_RATING_METRICS),
        # not filtered out here, so there's no way to accidentally re-include them.
        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"]
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
        yield Event(author=self.name, content=None)


# ---------------------------------------------------------------------------
# Root agent
# ---------------------------------------------------------------------------

root_agent = SequentialAgent(
    name="sonar_autofix_root",
    sub_agents=[SetupStep(), outer_loop, maintainability_expansion_loop, ReportStep()],
)

"""Phase IV — Push (Section 3 step 4 follow-through) + Report (Section 9)."""

import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .. import state_schema as sk
from ..tools import git_tools, sonar_tools
from ._shared import _msg
from .maintainability import _scanned_branch

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
        no_safe_fix_count = len(report["issues_no_safe_fix"])
        detail = f" — {no_safe_fix_count} declined as unsafe to auto-fix" if no_safe_fix_count else ""
        lines.append(f"- Flagged for manual review ({len(flagged)}){detail}:")
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
        # _scanned_branch: falls back to the default branch if this run's
        # own branch was never scanned (zero files fixed) -- see its
        # docstring for why get_quality_ratings(branch=BRANCH_NAME) would
        # otherwise silently return {} and misreport "all A".
        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], _scanned_branch(s)
        )
        all_a = all(v == "1.0" for v in ratings.values())
        report = {
            "branch_name": s[sk.BRANCH_NAME],
            "issues_fixed": s[sk.ISSUES_FIXED],
            "files_completed": s[sk.FILES_COMPLETED],
            "files_flagged_for_manual_review": s[sk.FILES_FLAGGED],
            # Subset of the issue_keys behind files_flagged_for_manual_review
            # specifically -- ones the model explicitly judged unsafe to
            # auto-fix (NO_SAFE_FIX), as opposed to attempted-but-broke-the-
            # build or attempted-but-verification-caught-a-miss. Lets a
            # reviewer tell "the model made a judgment call here" apart from
            # "the fix attempt itself failed" without parsing free-text
            # reasons in files_flagged_for_manual_review.
            "issues_no_safe_fix": s[sk.ISSUES_NO_SAFE_FIX],
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
        # Explicit state_delta, not just direct assignment -- confirmed
        # live: ADK's Runner only ever persists a state change into what
        # session_service.get_session() later returns via an event's own
        # actions.state_delta. Direct assignment on ctx.session.state
        # (what this used to do) is visible to any LATER step within the
        # SAME invocation (they share one live dict by reference), which
        # is why the actual pipeline never broke -- but this is the very
        # last event of the whole run, so there IS no later step to see
        # it, and run_local.py's own post-run
        # final_session.state.get("final_report") call came back None.
        s["final_report"] = report
        yield Event(
            author=self.name, content=_msg(_format_summary(report)),
            actions=EventActions(state_delta={"final_report": report}),
        )

"""Report step -- Sonar-rating-specific (security/reliability/maintainability
ratings). Push is generic and lives in core.agents.report; re-exported here
for convenience so callers only need one import for both."""

import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from core import state_schema as sk
from core.agents._shared import _msg
from core.agents.report import PushStep, _format_duration  # noqa: F401 -- re-exported

from sonar.tools import sonar_tools
from .maintainability import _scanned_branch

_RATING_LETTERS = {"1.0": "A", "2.0": "B", "3.0": "C", "4.0": "D", "5.0": "E"}


def _format_summary(report: dict) -> str:
    """Human-readable close-out message for ReportStep. Per-file detail is
    deliberately just a comma-joined list of file names, not a per-file
    breakdown -- the individual fix/checkpoint/re-scan events already
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


class ReportStep(BaseAgent):
    name: str = "report_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # security_rating/reliability_rating/sqale_rating ONLY -- duplication
        # and coverage are excluded at the tool level (IN_SCOPE_RATING_METRICS),
        # not filtered out here, so there's no way to accidentally re-include them.
        ratings = sonar_tools.get_quality_ratings(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], _scanned_branch(s)
        )
        all_a = all(v == "1.0" for v in ratings.values())
        report = {
            "branch_name": s[sk.BRANCH_NAME],
            "issues_fixed": s[sk.ISSUES_FIXED],
            "files_completed": s[sk.FILES_COMPLETED],
            "files_flagged_for_manual_review": s[sk.FILES_FLAGGED],
            "issues_no_safe_fix": s[sk.ISSUES_NO_SAFE_FIX],
            "outer_iterations": s[sk.OUTER_ITERATION],
            "hit_max_iterations": s[sk.OUTER_ITERATION] >= s[sk.MAX_OUTER_ITERATIONS],
            "checkpoints": s[sk.CHECKPOINTS],
            "final_ratings": ratings,           # e.g. {"security_rating": "1.0", ...}
            "all_categories_a": all_a,
            "wont_fix_review_queue": s[sk.WONT_FIX_REVIEW_QUEUE],  # human decides, agent never resolves these
            "push_result": s.get("temp:push_result", "not attempted"),
            "duration_seconds": time.time() - s.get(sk.RUN_START_TIME, time.time()),
            "tokens_consumed": s.get(sk.TOKEN_USAGE, {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}),
            "note_if_not_a": None if all_a else (
                "One or more categories are below A. Check "
                "wont_fix_review_queue (Minor/Low Security or Reliability "
                "issues awaiting human resolution) and files_flagged "
                "(entries tagged '<project-wide>' indicate the "
                "Maintainability debt ratio needs attention beyond "
                "single-issue fixes)."
            ),
        }
        # Explicit state_delta, not just direct assignment -- ADK's Runner
        # only ever persists a state change into what session_service.
        # get_session() later returns via an event's own actions.state_delta.
        s["final_report"] = report
        yield Event(
            author=self.name, content=_msg(_format_summary(report)),
            actions=EventActions(state_delta={"final_report": report}),
        )

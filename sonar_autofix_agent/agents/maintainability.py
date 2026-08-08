"""Maintainability debt-ratio top-up (post main-pass).

The main outer_loop only ever targets Critical/High/Medium Maintainability
issues, because sqale_rating is a debt-RATIO threshold (<=5% for A), not a
worst-issue gate — most projects hit A there without touching every Minor
code smell. This step only fires if the ratio is still over target after
the main pass, and pulls the highest-debt-minute Minor/Low smells first
rather than exhaustively closing the whole tail."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .. import state_schema as sk
from ..tools import sonar_tools
from ._shared import _msg
from .fix import PerFileLoopStep, _build_per_file_loop


def _scanned_branch(s: dict) -> str | None:
    """This run's own agent branch only gets its OWN Sonar analysis once a
    checkpoint's re-scan has actually run (TriggerAndReconcileScanStep) --
    which only happens after at least one file has been committed. A run
    that finds zero files to fix in the primary pass never fires a
    checkpoint, so the branch is never scanned at all: querying it by
    name for a rating/ratio either crashes (get_maintainability_debt_ratio
    correctly raises when the metric comes back missing) or silently lies
    (get_quality_ratings returns {}, and all(v == '1.0' for v in {}.values())
    is vacuously True in Python -- a report would falsely claim "all
    categories A" with zero actual data). Both confirmed live.

    Since a branch with zero commits so far is still byte-identical to
    whatever it was created from, the default branch's own already-
    existing rating/ratio (branch=None, the same "omit branch -> Sonar's
    own default" convention fetch_issues_and_hotspots already relies on)
    is a valid substitute -- no wasted extra scan needed.

    ce_edition additionally forces None regardless of FILES_COMPLETED:
    Community Edition has no branch-aware analysis at all (that's a
    Developer+ feature) -- trigger_sonar_analysis() already never passes
    -Dsonar.branch.name under ce_edition, so this run's own agent branch
    was NEVER created as a server-side entity, no matter how many files
    got committed. Confirmed live: a 5-file WebGoat run under CE_EDITION
    still 404'd on /api/measures/component for the agent's own branch
    name, because that branch simply doesn't exist in Sonar's data model
    under CE, full stop -- FILES_COMPLETED being non-empty was never a
    valid signal for CE the way it is for Developer+."""
    if s.get("ce_edition", True):
        return None
    return s[sk.BRANCH_NAME] if s[sk.FILES_COMPLETED] else None


class MaintainabilityDebtCheckStep(BaseAgent):
    name: str = "maintainability_debt_check_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        ratio = sonar_tools.get_maintainability_debt_ratio(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], _scanned_branch(s)
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
        # Same exclusion set as FetchPrioritizeStep (outer_loop.py) and for
        # the same reason -- a file already flagged in the main pass (e.g.
        # NO_SAFE_FIX declined) is out of scope here too, not a fresh
        # candidate just because it's being considered by a different loop.
        already_done = set(s[sk.FILES_COMPLETED]) | set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) \
            | {f["file"] for f in s[sk.FILES_FLAGGED]}
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

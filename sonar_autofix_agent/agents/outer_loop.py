"""Phase II — Fetch & Prioritize (Section 4) and the outer loop that
re-fetches after each pass (Section 5.5). Colocated because
FetchPrioritizeStep exists only to feed outer_loop's re-fetch cycle --
it's never used standalone."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .. import state_schema as sk
from ..tools import sonar_tools
from ._shared import _msg
from .fix import PerFileLoopStep, _build_per_file_loop


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

        # FILES_REVERTED_AT_CHECKPOINT and FILES_FLAGGED (not just
        # FILES_COMPLETED) are also excluded here -- a file whose fix
        # already broke the full build at a checkpoint, OR that ApplyAndVerifyStep
        # already gave up on this run (NO_SAFE_FIX decline, diff-apply
        # failure surviving a full-file retry, a still-failing compile/test
        # after its one retry), is out of scope for automatic re-attempt,
        # not just "not yet done". A FILES_FLAGGED entry doesn't always mean
        # "not done" -- see ApplyAndVerifyStep's "unresolved after patch"
        # path, which flags AND completes/commits the file in the same
        # pass -- but excluding on FILES_FLAGGED regardless is harmless in
        # that case, since FILES_COMPLETED already excludes it too; the
        # exclusion only matters for the cases where a flag was the file's
        # ONLY outcome. Without this, a re-fetch on the next outer_loop
        # iteration sees the file as simply missing from FILES_COMPLETED
        # and queues the exact same fix again -- confirmed live twice: (1)
        # spring-petclinic's VisitController.java S4684 fix broke 6
        # interconnected files' tests, got bisect-reverted, then got
        # reattempted identically on every subsequent outer_loop iteration;
        # (2) be-exps-portal's PasswordEncoderUtil.java (NO_SAFE_FIX-style
        # flags from ApplyAndVerifyStep, not a checkpoint revert) would have
        # been re-fetched and re-prompted with the identical issues on every
        # remaining outer_loop iteration once OuterExitCheck no longer used
        # FILES_FLAGGED itself to keep the loop alive (see that class) --
        # this is what actually stops the identical retry, not just the
        # loop-exit condition. FILES_FLAGGED already records the reason for
        # the final report regardless.
        excluded = set(s[sk.FILES_COMPLETED]) | set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) \
            | {f["file"] for f in s[sk.FILES_FLAGGED]}
        remaining = [g for g in ordered if g["file"] not in excluded]

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


class OuterExitCheck(BaseAgent):
    name: str = "outer_exit_check"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s[sk.OUTER_ITERATION] += 1
        # FILES_FLAGGED deliberately does NOT keep the loop going on its
        # own anymore -- now that FetchPrioritizeStep also excludes flagged
        # files from re-fetch (see its docstring), a non-empty FILES_FLAGGED
        # no longer means "there's more to do", just "something's already
        # been given up on for this run". Confirmed live: a run whose queue
        # genuinely emptied after iteration 1 (everything either fixed or
        # flagged) still burned 4 more full re-fetch cycles, each logging
        # "0 file(s) queued to fix", solely because FILES_FLAGGED stayed
        # non-empty -- pure waste, since nothing about state that would
        # change that outcome changes between iterations.
        remaining = bool(s[sk.ORDERED_FILES_REMAINING])
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

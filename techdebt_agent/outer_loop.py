"""Sonar-specific fetch/prioritize step, composed with core's generic
per-file-loop wrapper and exit check into the autofix agent's outer loop."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LoopAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from core import state_schema as sk
from core.agents._shared import _msg
from core.agents.fix_loop import PerFileLoopStep
from core.agents.outer_loop import OuterExitCheck  # noqa: F401 -- re-exported

from sonar.tools import sonar_tools
from .fix import _build_per_file_loop


class FetchPrioritizeStep(BaseAgent):
    name: str = "fetch_prioritize_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        # s.get("source_branch") if the run was pointed at a specific
        # branch, else None (the project's default branch, e.g. main) --
        # deliberately never s[sk.BRANCH_NAME]. The agent's own
        # {project_key}_agent_* branch has no Sonar analysis of its own
        # until the checkpoint scans it for the first time, so querying it
        # by name here 404s. Whichever branch this run actually started
        # from is the real "what needs fixing" source of truth -- SetupStep
        # already confirmed it has its own analysis if source_branch was
        # given. FILES_COMPLETED/FILES_FLAGGED (updated locally as files are
        # fixed/reverted) is what keeps re-fetching that same static list
        # from reprocessing already-handled files across outer_loop
        # iterations.
        issues = sonar_tools.fetch_issues_and_hotspots(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"], s.get("source_branch")
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
        # already broke the full build at a checkpoint, OR that
        # ApplyAndVerifyStep already gave up on this run, is out of scope
        # for automatic re-attempt, not just "not yet done". Without this,
        # a re-fetch on the next outer_loop iteration sees the file as
        # simply missing from FILES_COMPLETED and queues the exact same fix
        # again.
        excluded = set(s[sk.FILES_COMPLETED]) | set(s[sk.FILES_REVERTED_AT_CHECKPOINT]) \
            | {f["file"] for f in s[sk.FILES_FLAGGED]}
        remaining = [g for g in ordered if g["file"] not in excluded]

        # build_fix_prompt() needs rule_description per issue; fetched
        # lazily here, scoped to only the in-scope autofix issues actually
        # about to be prompted, and cached by rule_key for the rest of this
        # run (many issues share the same rule) -- not fetched for the
        # whole raw response, which would include out-of-scope and
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


def _build_outer_loop() -> LoopAgent:
    """Factory, not a module-level singleton -- see
    core.agents.fix_loop._build_fix_llm_agent's docstring for why."""
    return LoopAgent(
        name="outer_loop",
        sub_agents=[FetchPrioritizeStep(), PerFileLoopStep(loop=_build_per_file_loop()), OuterExitCheck()],
        max_iterations=5,  # hard ceiling backing MAX_OUTER_ITERATIONS in state
    )


outer_loop = _build_outer_loop()

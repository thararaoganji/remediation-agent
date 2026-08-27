"""Tool-agnostic report-step pieces: pushing the branch, and formatting a
duration. The actual report content (what got fixed, final ratings/metrics)
is tool-specific and lives in each tool package's own report step."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from .. import state_schema as sk
from ..tools import git_tools
from ._shared import _msg


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class PushStep(BaseAgent):
    """Runs after every fix loop for this run, i.e. after every committed
    file has already passed a checkpoint's full verify_build (CheckpointGate
    always fires once more when the file queue empties, so the last batch
    is never left un-checkpointed) -- by construction, every commit on the
    branch at this point belongs to a build that passed."""
    name: str = "push_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = s[sk.WORKING_DIR]
        branch_name = s[sk.BRANCH_NAME]

        # No files fixed and no checkpoints run means the branch has no new
        # commits over its base (e.g. the project was already clean) --
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

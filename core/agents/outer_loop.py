"""The tool-agnostic half of an outer fetch/re-fetch loop's exit condition.
Fetching and prioritizing what to fix next is inherently tool-specific
(Sonar issues, Veracode findings, ...), so that half lives in each tool
package; this just checks "is there anything left to do, or have we hit the
iteration cap" against the generic ORDERED_FILES_REMAINING/OUTER_ITERATION/
MAX_OUTER_ITERATIONS state, which every such loop shares regardless of
finding source."""

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from .. import state_schema as sk
from ._shared import _msg


class OuterExitCheck(BaseAgent):
    name: str = "outer_exit_check"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        s[sk.OUTER_ITERATION] += 1
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
                f"Outer loop iteration {s[sk.OUTER_ITERATION]} complete -- re-fetching."
            ))

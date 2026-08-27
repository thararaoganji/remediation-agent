"""
Conversational front door for the Sonar Auto-Fix Agent. See
sonar/intake.py's module docstring for the shared plumbing this builds on.

The rest of the graph (this package's pipeline_agent) is a deterministic
run-to-completion pipeline that expects `source` / `source_type` already
seeded in session.state before it starts -- it was never meant to be
chatted with. IntakeStep only invokes that LLM when source/source_type
aren't already in state, so a fully pre-seeded run (run_local.py, driven
entirely by .env) skips the conversation and falls straight into
pipeline_agent in the same turn.

IntakeStep is the package's root_agent directly -- NOT wrapped in a
SequentialAgent alongside pipeline_agent. SequentialAgent only checks
ctx.should_pause_invocation() between sub-agents, not event.actions.escalate
(that's LoopAgent-only), so a wrapper couldn't actually gate pipeline_agent
from running before the repo location was collected. IntakeStep invokes
pipeline_agent itself, manually, only once source/source_type are actually
in state.
"""

import datetime
import os
import tempfile
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from core import state_schema as sk
from core.adapters.base import BuildToolNotDetectedError, ToolNotAvailableError

from sonar import intake as shared_intake
from sonar.adapters import SonarConfigNotFoundError, SonarPreflightError
from .pipeline import pipeline_agent


class IntakeStep(BaseAgent):
    """Gate + state-seeding step. See module docstring."""

    name: str = "intake_step"
    # Shown in adk web's header/agent picker before any message is sent --
    # the closest thing to a pre-chat greeting the framework supports.
    description: str = (
        "Fetches SonarQube findings for a Java project (local or GitHub), "
        "fixes them file-by-file, verifies the build, and re-scans to "
        "confirm no regressions -- targeting Security/Reliability/"
        "Maintainability ratings of A. Send any message to begin."
    )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state

        if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
            # Deterministic, not LLM-authored, so what the agent claims
            # about its own scope/requirements can't drift or get
            # paraphrased inconsistently across runs.
            if len(ctx.session.events) <= 1:
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=shared_intake.WELCOME_MESSAGE)]),
                )
                return

            # Buffered, not forwarded live: intake_llm_agent's own text is
            # never shown to the user -- only its tool call matters. Still
            # actually run it so real answers ("here's my repo: owner/name")
            # get recognized; a non-text event (e.g. the function call/
            # response pair) is replayed as-is since those carry no
            # free-form model text.
            async for event in shared_intake.intake_llm_agent.run_async(ctx):
                if event.content and any(getattr(p, "text", None) for p in event.content.parts or []):
                    continue
                yield event

            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=shared_intake.SCOPE_REDIRECT_MESSAGE)]),
                )
                # Do NOT invoke pipeline_agent -- stop and wait for the next
                # user message.
                return

            # Valid repo captured -- a fixed confirmation instead of the
            # (suppressed) model text, so the user still sees that intake
            # actually succeeded.
            branch_note = f" on branch `{s['source_branch']}`" if s.get("source_branch") else ""
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    f"Got it — analyzing the {s[sk.SOURCE_TYPE]} repo at "
                    f"{s['source']}{branch_note}. Starting the Sonar analysis now."
                ))]),
            )

        missing = [k for k in shared_intake.REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    "Sorry, I can't start the analysis yet — the server is "
                    f"missing required configuration: {', '.join(missing)}. "
                    "Please ask an administrator to set these in .env."
                ))]),
            )
            return

        # SONAR_PROJECT_KEY is deliberately NOT seeded from .env here --
        # SetupStep reads it straight from build.gradle/pom.xml once the
        # source is checked out, since that's the value the Sonar plugin
        # invocation actually uses.
        s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
        s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
        s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
        s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
        s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
        # setdefault, not direct assignment: a chat user's "on the develop
        # branch" (captured via set_analysis_source) must win over .env's
        # SOURCE_BRANCH, the same precedence source/source_type already
        # have over their own .env equivalents.
        s.setdefault("source_branch", os.environ.get("SOURCE_BRANCH") or None)
        # tempfile.gettempdir() rather than a hardcoded "/tmp" -- that path
        # doesn't exist on Windows; gettempdir() resolves to the right
        # per-OS temp location automatically.
        s.setdefault("workspace_root", os.environ.get(
            "WORKSPACE_ROOT", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
        ))
        # Not setdefault: every invocation of IntakeStep that reaches this
        # point is about to trigger a fresh pipeline_agent run against a
        # freshly re-cloned workspace -- the branch name it creates should
        # reflect when THIS run started, not get stuck on whatever
        # timestamp happened to be cached in session.state from an earlier
        # run in the same chat session. datetime.now() (machine-local
        # time), not utcnow() -- branch names are read by humans, who
        # expect the time on their own clock, not UTC.
        s["timestamp"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Scoped to the pipeline run itself (not any back-and-forth spent
        # above resolving the repo) -- that's what "duration/tokens of the
        # analysis" in the final report actually means.
        s[sk.RUN_START_TIME] = time.time()
        s[sk.TOKEN_USAGE] = {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}

        try:
            async for event in pipeline_agent.run_async(ctx):
                shared_intake._accumulate_tokens(s, event)
                yield event
        except (
            ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
        ) as e:
            # SetupStep's preflight checks all deliberately raise before any
            # issue fetch or LLM call and are NOT caught inside the agent
            # package -- this is the one place that turns that into a clean
            # chat message instead of an unhandled exception reaching adk
            # web's request handler.
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"Analysis stopped: {e}")]),
            )

import datetime
import os
import tempfile
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from sonar_autofix_agent import state_schema as sk
from sonar_autofix_agent.adapters.base import (
    ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
)
from sonar_autofix_agent.intake import (
    REQUIRED_ENV, COVERAGE_WELCOME_MESSAGE, SCOPE_REDIRECT_MESSAGE,
    intake_llm_agent, _accumulate_tokens
)

from .enhance_coverage import coverage_pipeline

class CoverageIntakeStep(BaseAgent):
    """Conversational front-door for the Sonar Coverage-Enhance Agent."""

    name: str = "coverage_intake_step"
    description: str = (
        "Finds uncovered code paths/branches in a Java project (local or GitHub), "
        "and automatically generates targeted JUnit unit tests to boost test coverage. "
        "Send any message to begin."
    )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state

        if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
            if len(ctx.session.events) <= 1:
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=COVERAGE_WELCOME_MESSAGE)]),
                )
                return

            async for event in intake_llm_agent.run_async(ctx):
                if event.content and any(getattr(p, "text", None) for p in event.content.parts or []):
                    continue
                yield event

            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=SCOPE_REDIRECT_MESSAGE)]),
                )
                return

            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    f"Got it — analyzing the {s[sk.SOURCE_TYPE]} repo at "
                    f"{s['source']}. Starting the coverage enhancement now."
                ))]),
            )

        missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
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

        s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
        s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
        s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
        s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
        s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
        s.setdefault("workspace_root", os.environ.get(
            "WORKSPACE_ROOT", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
        ))
        s["timestamp"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        s[sk.RUN_START_TIME] = time.time()
        s[sk.TOKEN_USAGE] = {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}

        try:
            async for event in coverage_pipeline.run_async(ctx):
                _accumulate_tokens(s, event)
                yield event
        except (
            ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
        ) as e:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"Analysis stopped: {e}")]),
            )

root_agent = CoverageIntakeStep(sub_agents=[coverage_pipeline])

__all__ = ["root_agent"]

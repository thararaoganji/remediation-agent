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
from .fix_duplicate import duplicate_pipeline


class DuplicateIntakeStep(BaseAgent):
    """Conversational front-door for the Sonar Duplicate-Fix Agent."""

    name: str = "duplicate_intake_step"
    description: str = (
        "Detects code duplication in a Java project (local or GitHub), "
        "and automatically extracts shared logic into clean, reusable "
        "helper classes/methods. Send any message to begin."
    )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state

        if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
            if len(ctx.session.events) <= 1:
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=shared_intake.DUPLICATE_WELCOME_MESSAGE)]),
                )
                return

            async for event in shared_intake.intake_llm_agent.run_async(ctx):
                if event.content and any(getattr(p, "text", None) for p in event.content.parts or []):
                    continue
                yield event

            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=shared_intake.SCOPE_REDIRECT_MESSAGE)]),
                )
                return

            branch_note = f" on branch `{s['source_branch']}`" if s.get("source_branch") else ""
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    f"Got it — analyzing the {s[sk.SOURCE_TYPE]} repo at "
                    f"{s['source']}{branch_note}. Starting the duplicate logic extraction now."
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

        s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
        s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
        s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
        s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
        s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
        s.setdefault("source_branch", os.environ.get("SOURCE_BRANCH") or None)
        s.setdefault("workspace_root", os.environ.get(
            "WORKSPACE_ROOT", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
        ))
        s["timestamp"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        s[sk.RUN_START_TIME] = time.time()
        s[sk.TOKEN_USAGE] = {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}

        try:
            async for event in duplicate_pipeline.run_async(ctx):
                shared_intake._accumulate_tokens(s, event)
                yield event
        except (
            ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
        ) as e:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"Analysis stopped: {e}")]),
            )


root_agent = DuplicateIntakeStep(sub_agents=[duplicate_pipeline])

__all__ = ["root_agent"]

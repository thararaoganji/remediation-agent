"""
Runs the sonar-autofix root_agent directly via ADK's Runner, seeding the
session state this pipeline actually needs from .env. `adk web` / `adk run`
are built for chatting with an agent turn-by-turn — this pipeline isn't
conversational, it's a deterministic run-to-completion job, so driving it
from a script that pre-populates state is the accurate way to exercise it.

NOTE: google-adk's Runner/SessionService API has shifted across versions.
If any call below doesn't match your installed version, check
https://google.github.io/adk-docs/ for that version's Runner signature —
treat this script as a starting point, not a guaranteed-stable API surface.
"""

import asyncio
import datetime
import os
import sys
import tempfile

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from sonar_autofix_agent import root_agent
from sonar_autofix_agent.adapters.base import (
    ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError,
)

load_dotenv()

APP_NAME = "sonar_autofix"
USER_ID = "local_dev"
SESSION_ID = "local_run_1"

REQUIRED = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "LANGUAGE"]


def build_initial_state() -> dict:
    source_type = os.environ.get("SOURCE_TYPE", "local")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if source_type == "github":
        if not os.environ.get("GITHUB_REPO"):
            missing.append("GITHUB_REPO")
    elif source_type == "local":
        if not os.environ.get("SOURCE_PATH"):
            missing.append("SOURCE_PATH")
    else:
        sys.exit(f"SOURCE_TYPE must be 'local' or 'github', got {source_type!r}")

    if missing:
        sys.exit(f"Missing required .env keys: {', '.join(missing)}")

    source = os.environ["GITHUB_REPO"] if source_type == "github" else os.environ["SOURCE_PATH"]

    return {
        "source": source,
        "source_type": source_type,
        # sonar_project_key deliberately NOT seeded here — SetupStep reads
        # it from build.gradle/pom.xml once the source is checked out.
        "language": os.environ["LANGUAGE"],
        "sonar_base_url": os.environ["SONAR_BASE_URL"],
        "sonar_token": os.environ["SONAR_TOKEN"],
        "ce_edition": os.environ.get("CE_EDITION", "true").lower() == "true",
        "github_token": os.environ.get("GITHUB_TOKEN") or None,
        "workspace_root": os.environ.get(
            "WORKSPACE_ROOT", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
        ),
        # machine-local time, not UTC -- branch names are read by humans,
        # who expect the time on their own clock.
        "timestamp": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
    }


async def main():
    initial_state = build_initial_state()

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID, state=initial_state
    )

    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    # SequentialAgent/LoopAgent-only graphs don't need real user text to
    # drive them — this message just triggers the first turn.
    trigger = types.Content(role="user", parts=[types.Part(text="start")])

    try:
        async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=trigger):
            # Some ADK versions surface an agent-raised exception as an
            # error-bearing Event instead of letting it propagate as a
            # Python exception — check both paths rather than assuming one.
            err = getattr(event, "error_message", None)
            if err:
                sys.exit(f"\nStopped: {err}")
            print(f"[{event.author}] {event.content or '(state update)'}")
    except (ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError) as e:
        # Raised by SetupStep's preflight check before any Sonar fetch or
        # LLM call — surface it as a clean stop, not a stack trace.
        sys.exit(f"\nStopped: {e}")

    final_session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    print("\n--- final_report ---")
    print(final_session.state.get("final_report"))


if __name__ == "__main__":
    asyncio.run(main())

"""
Runs the agent_techdebt root_agent directly via ADK's Runner, seeding the
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

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent_techdebt import AGENT_SLUG, root_agent
from core.adapters.base import ToolNotAvailableError, BuildToolNotDetectedError
from core.tools import git_tools
from sonar.adapters import SonarConfigNotFoundError, SonarPreflightError

load_dotenv()

APP_NAME = "sonar_remediation"
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
        # Optional -- omit or leave unset to use the repo's default branch,
        # same as before this existed. When set, both the checked-out code
        # AND the Sonar issues fetched come from this branch specifically
        # (see SetupStep's preflight check for the "this branch has never
        # been analyzed" failure mode this guards against).
        "source_branch": os.environ.get("SOURCE_BRANCH") or None,
        "agent_slug": AGENT_SLUG,
        # Per-agent clone isolation for github source -- this agent's own
        # sibling workspace dir (sonar_remediation_<slug>/), not one shared
        # across all three agents. No effect on local source (edited in place).
        "workspace_root": git_tools.agent_workspace_root(
            os.environ.get("WORKSPACE_ROOT") or git_tools.DEFAULT_WORKSPACE_ROOT, AGENT_SLUG
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
    except (
        ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
        RuntimeError, TimeoutError,
    ) as e:
        # Mirrors sonar/intake.py's build_intake_step catch list -- belt and
        # suspenders in case an exception ever reaches this level instead of
        # being caught inside the intake step's own try/except (the normal
        # path). Without this, a mid-run failure (a Sonar scan that failed,
        # a checkpoint that couldn't recover, git push with no origin) would
        # print a raw Python traceback instead of a clean, actionable line.
        sys.exit(f"\nStopped: {e}")

    final_session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    print("\n--- final_report ---")
    print(final_session.state.get("final_report"))


if __name__ == "__main__":
    asyncio.run(main())

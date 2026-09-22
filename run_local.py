"""
Runs one of the three root_agents (agent_techdebt / agent_coverage /
agent_duplicate, picked via the AGENT_TYPE env var) directly via ADK's
Runner, seeding the session state this pipeline actually needs from .env.
`adk web` / `adk run` are built for chatting with an agent turn-by-turn —
this pipeline isn't conversational, it's a deterministic run-to-completion
job, so driving it from a script that pre-populates state is the accurate
way to exercise it.

NOTE: google-adk's Runner/SessionService API has shifted across versions.
If any call below doesn't match your installed version, check
https://google.github.io/adk-docs/ for that version's Runner signature —
treat this script as a starting point, not a guaranteed-stable API surface.
"""

import asyncio
import datetime
import importlib
import os
import sys

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from core import state_schema as sk
from core.adapters.base import ToolNotAvailableError, BuildToolNotDetectedError
from core.tools import git_tools, run_status
from sonar.adapters import SonarConfigNotFoundError, SonarPreflightError
from sonar.tools import sonar_tools

load_dotenv()

APP_NAME = "sonar_remediation"
USER_ID = "local_dev"
SESSION_ID = "local_run_1"

REQUIRED = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "LANGUAGE"]

AGENT_MODULES = {
    "techdebt": "agent_techdebt",
    "coverage": "agent_coverage",
    "duplicate": "agent_duplicate",
}


def load_agent():
    agent_type = os.environ.get("AGENT_TYPE", "techdebt")
    module_name = AGENT_MODULES.get(agent_type)
    if module_name is None:
        sys.exit(f"AGENT_TYPE must be one of {sorted(AGENT_MODULES)}, got {agent_type!r}")
    module = importlib.import_module(module_name)
    return module.AGENT_SLUG, module.root_agent


def build_initial_state(agent_slug: str) -> dict:
    # GitHub is the only supported source -- the agent always clones fresh
    # into its own tmp workspace (git_tools.resolve_source), never edits a
    # local checkout in place.
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if not os.environ.get("GITHUB_REPO"):
        missing.append("GITHUB_REPO")

    if missing:
        sys.exit(f"Missing required .env keys: {', '.join(missing)}")

    return {
        "source": os.environ["GITHUB_REPO"],
        "source_type": "github",
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
        "agent_slug": agent_slug,
        # Per-agent clone isolation -- this agent's own sibling workspace
        # dir (sonar_remediation_<slug>/), not one shared across all three
        # agents.
        "workspace_root": git_tools.agent_workspace_root(
            os.environ.get("WORKSPACE_ROOT") or git_tools.DEFAULT_WORKSPACE_ROOT, agent_slug
        ),
        # machine-local time, not UTC -- branch names are read by humans,
        # who expect the time on their own clock.
        "timestamp": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
    }


async def main():
    agent_slug, root_agent = load_agent()
    initial_state = build_initial_state(agent_slug)
    # Set by the (planned) web dashboard when it triggers a Cloud Run Job
    # execution -- see core/tools/run_status.py. Unset for every existing
    # way of running this (bare `python run_local.py`, `gcloud run jobs
    # execute` without overrides), in which case every run_status call
    # below is a no-op and behavior is completely unchanged.
    run_id = os.environ.get("RUN_ID")
    run_status.report_started(
        run_id, agent_slug, initial_state["source_type"], initial_state["source"],
        initial_state.get("source_branch"),
    )

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID, state=initial_state
    )

    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=session_service)

    # SequentialAgent/LoopAgent-only graphs don't need real user text to
    # drive them — this message just triggers the first turn.
    trigger = types.Content(role="user", parts=[types.Part(text="start")])

    # Branch name (and the project key needed to link to it) only exist
    # once SetupStep runs -- reported to the dashboard once, the first
    # time both show up in state, rather than precomputed (impossible,
    # since git_tools.create_branch() bakes in a run-time timestamp) or
    # polled on every single event (unnecessary Firestore writes).
    branch_reported = False

    async def _maybe_report_branch() -> None:
        nonlocal branch_reported
        if branch_reported or not run_id:
            return
        session = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)
        branch_name = session.state.get(sk.BRANCH_NAME)
        project_key = session.state.get(sk.SONAR_PROJECT_KEY)
        if branch_name and project_key:
            branch_reported = True
            run_status.report_branch_ready(
                run_id, branch_name, sonar_tools.dashboard_url(initial_state["sonar_base_url"], project_key, branch_name)
            )

    try:
        async for event in runner.run_async(user_id=USER_ID, session_id=SESSION_ID, new_message=trigger):
            # Some ADK versions surface an agent-raised exception as an
            # error-bearing Event instead of letting it propagate as a
            # Python exception — check both paths rather than assuming one.
            err = getattr(event, "error_message", None)
            if err:
                run_status.report_finished(run_id, "failed", error=err)
                sys.exit(f"\nStopped: {err}")
            print(f"[{event.author}] {event.content or '(state update)'}")
            await _maybe_report_branch()
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
        run_status.report_finished(run_id, "failed", error=str(e))
        sys.exit(f"\nStopped: {e}")
    except Exception as e:
        # Anything NOT in the expected list above is a genuine bug, not a
        # clean stop -- still re-raised (unchanged crash/traceback
        # behavior) but reported first so a run stuck mid-pipeline shows
        # up as "failed" on the dashboard instead of "running" forever.
        run_status.report_finished(run_id, "failed", error=str(e))
        raise

    final_session = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    final_report = final_session.state.get("final_report")
    run_status.report_finished(run_id, "succeeded", final_report=final_report)
    print("\n--- final_report ---")
    print(final_report)


if __name__ == "__main__":
    asyncio.run(main())

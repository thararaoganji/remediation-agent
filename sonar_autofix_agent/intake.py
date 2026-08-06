"""
Conversational front door for the Sonar Auto-Fix Agent.

The rest of the graph (agents.py's pipeline_agent) is a deterministic
run-to-completion pipeline that expects `source` / `source_type` already
seeded in session.state before it starts — it was never meant to be chatted
with. This module adds the one conversational step this project actually
needs: ask the user for a local path or GitHub repo, then hand off.

`intake_llm_agent` is deliberately narrow — it has exactly one tool and an
instruction that refuses everything else (see INTAKE_INSTRUCTION). It is
NOT a general coding assistant.

IntakeStep only invokes that LLM when source/source_type aren't already in
state, so a fully pre-seeded run (run_local.py, driven entirely by .env)
skips the conversation and falls straight into pipeline_agent in the same
turn.

IntakeStep is the package's root_agent directly (see __init__.py) — it is
NOT wrapped in a SequentialAgent alongside pipeline_agent. That was the
original design and it had a real bug: SequentialAgent in this ADK version
only checks ctx.should_pause_invocation() between sub-agents, not
event.actions.escalate — escalate is a LoopAgent-only signal. So yielding
escalate=True from a sub-agent inside a plain SequentialAgent does nothing
to stop it; the next sub-agent (pipeline_agent's SetupStep) still ran in
the same turn and crashed on a missing `source` key. IntakeStep now invokes
pipeline_agent itself — manually, via .run_async(ctx), the same pattern
CheckpointGate already uses for checkpoint_pipeline in agents.py — only
once source/source_type are actually in state. When they aren't, it simply
returns without invoking anything further, which is the only thing that
actually stops execution for this turn.
"""

import datetime
import os
import tempfile
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.tools import ToolContext
from google.genai import types

from . import state_schema as sk
from .adapters.base import (
    ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
)
from .agents import pipeline_agent

REQUIRED_ENV = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "LANGUAGE"]

WELCOME_MESSAGE = """\
I'm the Sonar Auto-Fix Agent. I fix SonarQube findings in a Java project \
— local or GitHub — file by file, verifying the build and re-scanning \
after each batch so nothing regresses.

Which repo would you like me to analyze — a local path, or the full GitHub \
repo URL (the repo whose root contains build.gradle or pom.xml)?"""

# Shown verbatim, every time, whenever the user's message doesn't resolve
# to a valid repo — see IntakeStep. Fixed and deterministic rather than
# whatever intake_llm_agent might phrase, so the scope restriction and
# "politely decline" behavior are guaranteed, not just instructed and
# hoped-for.
SCOPE_REDIRECT_MESSAGE = (
    "I can only help start a Sonar analysis — I'm not able to help with "
    "anything else. Please share a local path or the full GitHub repo URL "
    "(the repo whose root contains build.gradle or pom.xml) you'd like analyzed."
)

INTAKE_INSTRUCTION = """
Your only job: read the user's message and decide whether it identifies a
repository to run a Sonar analysis against — a local filesystem path, or a
GitHub repo ("owner/repo" or a full URL).

If it clearly does, call `set_analysis_source` with:
- source_type: "local" or "github"
- source: the path (if local) or "owner/repo"/URL (if github)

If it doesn't (greetings, unrelated questions, coding requests, anything
ambiguous with no clear local-vs-GitHub signal) — do NOT call the tool, and
do not attempt to answer, help with, or engage with the message in any
other way. The caller replaces your response with a fixed message in that
case, so nothing else about your reply matters — just don't call the tool
unless you have a real path or repo.
"""


def _accumulate_tokens(state: dict, event: Event) -> None:
    """Event extends google-adk's LlmResponse, so every event carries a
    usage_metadata field — populated only on events that actually came
    back from a model call (fix_llm_agent, one per file, is the dominant
    cost here), None on every deterministic BaseAgent step's own events.
    IntakeStep re-yields every event pipeline_agent produces, including
    ones from agents nested arbitrarily deep in outer_loop/per_file_loop/
    checkpoint_pipeline, so hooking this one spot sees the whole run."""
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return
    totals = state[sk.TOKEN_USAGE]
    totals["prompt_tokens"] += usage.prompt_token_count or 0
    totals["candidates_tokens"] += usage.candidates_token_count or 0
    totals["total_tokens"] += usage.total_token_count or 0


def set_analysis_source(source_type: str, source: str, tool_context: ToolContext) -> dict:
    """Record the repository to analyze and mark intake complete.

    Args:
        source_type: "local" or "github".
        source: absolute or ~-relative local path (for "local"), or
            "owner/repo" / a full GitHub URL (for "github").
    """
    source_type = source_type.strip().lower()
    if source_type not in ("local", "github"):
        return {"status": "error", "message": "source_type must be 'local' or 'github'."}

    source = source.strip()
    if source_type == "local":
        expanded = os.path.expanduser(source)
        if not os.path.isdir(expanded):
            return {
                "status": "error",
                "message": f"'{expanded}' is not a directory that exists on this machine.",
            }
        source = expanded

    tool_context.state["source"] = source
    tool_context.state[sk.SOURCE_TYPE] = source_type
    return {"status": "ok", "source_type": source_type, "source": source}


intake_llm_agent = LlmAgent(
    name="intake_agent",
    model="gemini-flash-latest",
    instruction=INTAKE_INSTRUCTION,
    tools=[set_analysis_source],
)


class IntakeStep(BaseAgent):
    """Gate + state-seeding step. See module docstring."""

    name: str = "intake_step"
    # Shown in adk web's header/agent picker before any message is sent —
    # the closest thing to a pre-chat greeting the framework supports.
    # WELCOME_MESSAGE itself only fires in response to the first message,
    # since agent execution is always reactive to a user turn; there's no
    # ADK hook for "run something when the session/page opens."
    description: str = (
        "Fetches SonarQube findings for a Java project (local or GitHub), "
        "fixes them file-by-file, verifies the build, and re-scans to "
        "confirm no regressions — targeting Security/Reliability/"
        "Maintainability ratings of A. Send any message to begin."
    )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state

        if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
            # Deterministic, not LLM-authored, so what the agent claims
            # about its own scope/requirements can't drift or get
            # paraphrased inconsistently across runs. Shown as the complete
            # response to the very first turn (no prior history in this
            # session) rather than alongside an LLM call in the same turn —
            # the banner already ends by asking for the repo, so running
            # the LLM right after would mean asking it to generate a turn
            # immediately following its own just-emitted message, with no
            # new user input in between. The user's actual answer (or an
            # off-topic ask) gets handled by intake_llm_agent starting next
            # turn, same as any other follow-up.
            if len(ctx.session.events) <= 1:
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=WELCOME_MESSAGE)]),
                )
                return

            # Buffered, not forwarded live: intake_llm_agent's own text is
            # never shown to the user (see SCOPE_REDIRECT_MESSAGE above) —
            # only its tool call matters. Still actually run it so real
            # answers ("here's my repo: owner/name") get recognized; a
            # non-text event (e.g. the function call/response pair) is
            # replayed as-is since those carry no free-form model text.
            async for event in intake_llm_agent.run_async(ctx):
                if event.content and any(getattr(p, "text", None) for p in event.content.parts or []):
                    continue
                yield event

            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                yield Event(
                    author=self.name,
                    content=types.Content(role="model", parts=[types.Part(text=SCOPE_REDIRECT_MESSAGE)]),
                )
                # Do NOT invoke pipeline_agent — stop and wait for the next
                # user message.
                return

            # Valid repo captured — same reasoning as SCOPE_REDIRECT_MESSAGE:
            # a fixed confirmation instead of the (suppressed) model text,
            # so the user still sees that intake actually succeeded.
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    f"Got it — analyzing the {s[sk.SOURCE_TYPE]} repo at "
                    f"{s['source']}. Starting the Sonar analysis now."
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

        # SONAR_PROJECT_KEY is deliberately NOT seeded from .env here —
        # SetupStep reads it straight from build.gradle/pom.xml once the
        # source is checked out, since that's the value the Sonar plugin
        # invocation actually uses.
        s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
        s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
        s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
        s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
        s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
        # tempfile.gettempdir() rather than a hardcoded "/tmp" — that path
        # doesn't exist on Windows; gettempdir() resolves to the right
        # per-OS temp location (TEMP/TMP env vars on Windows, /tmp on
        # Unix-likes) automatically.
        s.setdefault("workspace_root", os.environ.get(
            "WORKSPACE_ROOT", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
        ))
        # Not setdefault: every invocation of IntakeStep that reaches this
        # point is about to trigger a fresh pipeline_agent run against a
        # freshly re-cloned workspace (see git_tools.resolve_source) — the
        # branch name it creates should reflect when THIS run started, not
        # get stuck on whatever timestamp happened to be cached in
        # session.state from an earlier run in the same chat session.
        # datetime.now() (machine-local time), not utcnow() — branch names
        # are read by humans, who expect the time on their own clock, not UTC.
        s["timestamp"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Scoped to the pipeline run itself (not any back-and-forth spent
        # above resolving the repo) — that's what "duration/tokens of the
        # analysis" in the final report actually means.
        s[sk.RUN_START_TIME] = time.time()
        s[sk.TOKEN_USAGE] = {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}

        try:
            async for event in pipeline_agent.run_async(ctx):
                _accumulate_tokens(s, event)
                yield event
        except (
            ToolNotAvailableError, BuildToolNotDetectedError, SonarConfigNotFoundError, SonarPreflightError,
        ) as e:
            # SetupStep's preflight checks (build tool on PATH, pom.xml/
            # build.gradle present, Sonar project key configured, Sonar
            # server reachable with a valid token and an actual analysis
            # under this project key) all deliberately raise before any
            # issue fetch or LLM call and
            # deliberately are NOT caught inside agents.py — this is the
            # one place that turns that into a clean chat message instead
            # of an unhandled exception reaching adk web's request handler.
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=f"Analysis stopped: {e}")]),
            )

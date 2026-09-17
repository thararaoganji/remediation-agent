"""
Shared conversational-intake plumbing for all three Sonar agents.

Every agent (tech-debt / coverage / duplication) has the same front door:
a deterministic welcome message, one narrow LLM turn that only extracts a
repo location from the user's reply, a fixed scope-redirect for anything
off-topic, then it seeds `session.state` from `.env` and hands off to that
agent's own deterministic pipeline. Only three things differ per agent --
the welcome text, the one-line "starting now" confirmation, and which
pipeline it hands off to -- so `build_intake_step()` takes exactly those
and returns the finished root agent.

A fully pre-seeded run (`run_local.py`, driven entirely by `.env`) already
has `source` / `source_type` in state, so the intake step skips the whole
conversation and falls straight into the pipeline in the same turn.

The intake step IS each package's `root_agent` directly -- not wrapped in a
SequentialAgent alongside the pipeline. SequentialAgent only checks
`ctx.should_pause_invocation()` between sub-agents, not
`event.actions.escalate` (that is LoopAgent-only), so a wrapper could not
actually gate the pipeline from running before the repo location was
collected. The intake step invokes the pipeline itself, manually, only
once `source` / `source_type` are in state.
"""

import datetime
import os
import time
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.tools import ToolContext
from google.genai import types

from core import state_schema as sk
from core.adapters.base import BuildToolNotDetectedError, ToolNotAvailableError
from core.tools import git_tools
from sonar.adapters import SonarConfigNotFoundError, SonarPreflightError

REQUIRED_ENV = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "LANGUAGE"]

BRANCH_HINT = (
    "\n\nBy default I use the repo's default branch. If you want a "
    "different one, just say so (e.g. \"the develop branch\" or "
    "\"release/v2\")."
)

REPO_PROMPT = (
    "Which repo would you like me to analyze — a local path, or the full "
    "GitHub repo URL (the repo whose root contains build.gradle or pom.xml)?"
)

# Shown verbatim, every time, whenever the user's message doesn't resolve
# to a valid repo. Fixed and deterministic rather than whatever
# intake_llm_agent might phrase, so the scope restriction and "politely
# decline" behavior are guaranteed, not just instructed and hoped-for.
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
- source_branch: ONLY if the user names a specific branch to use instead of
  the repo's default (e.g. "on the develop branch", "from release/v2",
  "use the feature/x branch") — pass exactly the branch name they gave,
  nothing else. If they don't mention a branch at all, omit this argument
  entirely — do NOT guess a branch name or default to "main"/"master"
  yourself; the caller already has its own correct default for that case.

If it doesn't (greetings, unrelated questions, coding requests, anything
ambiguous with no clear local-vs-GitHub signal) — do NOT call the tool, and
do not attempt to answer, help with, or engage with the message in any
other way. The caller replaces your response with a fixed message in that
case, so nothing else about your reply matters — just don't call the tool
unless you have a real path or repo.
"""


def _model_msg(author: str, text: str) -> Event:
    return Event(author=author, content=types.Content(role="model", parts=[types.Part(text=text)]))


def _accumulate_tokens(state: dict, event: Event) -> None:
    """Event extends google-adk's LlmResponse, so every event carries a
    usage_metadata field — populated only on events that actually came
    back from a model call (fix_llm_agent, one per file, is the dominant
    cost here), None on every deterministic BaseAgent step's own events.
    The intake step re-yields every event its pipeline produces, including
    ones from agents nested arbitrarily deep in outer/per-file/checkpoint
    loops, so hooking this one spot sees the whole run."""
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return
    totals = state[sk.TOKEN_USAGE]
    totals["prompt_tokens"] += usage.prompt_token_count or 0
    totals["candidates_tokens"] += usage.candidates_token_count or 0
    totals["total_tokens"] += usage.total_token_count or 0


def set_analysis_source(
    source_type: str, source: str, tool_context: ToolContext, source_branch: str | None = None,
) -> dict:
    """Record the repository (and optionally a specific branch) to analyze,
    and mark intake complete.

    Args:
        source_type: "local" or "github".
        source: absolute or ~-relative local path (for "local"), or
            "owner/repo" / a full GitHub URL (for "github").
        source_branch: a specific branch to use instead of the repo's
            default, if the user named one. Omit if they didn't -- None
            here means "use the default branch", not "unset".
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
    tool_context.state["source_branch"] = source_branch.strip() if source_branch else None
    return {
        "status": "ok", "source_type": source_type, "source": source,
        "source_branch": tool_context.state["source_branch"] or "(default branch)",
    }


intake_llm_agent = LlmAgent(
    name="intake_agent",
    model="gemini-flash-latest",
    instruction=INTAKE_INSTRUCTION,
    tools=[set_analysis_source],
)


def build_intake_step(
    *, step_name: str, description: str, welcome_message: str, start_phrase: str,
    pipeline: BaseAgent, agent_slug: str,
) -> BaseAgent:
    """Build a package's `root_agent`: the shared intake gate wired to one
    agent's welcome text, hand-off confirmation, pipeline, and slug (which
    isolates its github clone dir -- see git_tools.agent_workspace_root).
    See the module docstring for why the intake step is the root agent
    directly."""

    # Bound to differently-named locals: a class body treats any name it
    # assigns (`description = ...`) as local, which would shadow the
    # same-named parameter on the right-hand side.
    _name, _description = step_name, description

    class _IntakeStep(BaseAgent):
        # `description` is shown in adk web's header/agent picker before any
        # message is sent -- the closest thing to a pre-chat greeting the
        # framework supports.
        name: str = _name
        description: str = _description

        async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
            s = ctx.session.state

            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                # Deterministic, not LLM-authored, so what the agent claims
                # about its own scope/requirements can't drift across runs.
                if len(ctx.session.events) <= 1:
                    yield _model_msg(self.name, welcome_message)
                    return

                # Buffered, not forwarded live: intake_llm_agent's own text
                # is never shown -- only its tool call matters. Still run it
                # so real answers ("here's my repo: owner/name") get
                # recognized; non-text events (the function call/response
                # pair) are replayed as-is since they carry no model prose.
                async for event in intake_llm_agent.run_async(ctx):
                    if event.content and any(getattr(p, "text", None) for p in event.content.parts or []):
                        continue
                    yield event

                if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                    yield _model_msg(self.name, SCOPE_REDIRECT_MESSAGE)
                    # Do NOT invoke the pipeline -- wait for the next message.
                    return

                # Valid repo captured -- a fixed confirmation instead of the
                # (suppressed) model text, so the user still sees that
                # intake actually succeeded.
                branch_note = f" on branch `{s['source_branch']}`" if s.get("source_branch") else ""
                yield _model_msg(self.name, (
                    f"Got it — analyzing the {s[sk.SOURCE_TYPE]} repo at "
                    f"{s['source']}{branch_note}. {start_phrase}"
                ))

            missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
            if missing:
                yield _model_msg(self.name, (
                    "Sorry, I can't start the analysis yet — the server is "
                    f"missing required configuration: {', '.join(missing)}. "
                    "Please ask an administrator to set these in .env."
                ))
                return

            # SONAR_PROJECT_KEY is deliberately NOT seeded from .env --
            # SetupStep reads it straight from build.gradle/pom.xml once the
            # source is checked out, since that's the value the Sonar plugin
            # invocation actually uses.
            #
            # setdefault, not direct assignment: a chat user's "on the
            # develop branch" (captured via set_analysis_source) must win
            # over .env's SOURCE_BRANCH, the same precedence source /
            # source_type already have over their own .env equivalents.
            s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
            s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
            s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
            s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
            s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
            s.setdefault("source_branch", os.environ.get("SOURCE_BRANCH") or None)
            s[sk.AGENT_SLUG] = agent_slug
            # Per-agent clone isolation for github source: this agent's own
            # sibling workspace dir, not one shared across all three agents.
            # DEFAULT_WORKSPACE_ROOT uses tempfile.gettempdir(), not a
            # hardcoded "/tmp" -- that path doesn't exist on Windows.
            s.setdefault("workspace_root", git_tools.agent_workspace_root(
                os.environ.get("WORKSPACE_ROOT") or git_tools.DEFAULT_WORKSPACE_ROOT, agent_slug
            ))
            # Not setdefault: every invocation that reaches this point is
            # about to trigger a fresh pipeline run against a freshly
            # re-cloned workspace -- the branch name it creates should
            # reflect when THIS run started, not a timestamp cached from an
            # earlier run in the same chat session. Machine-local time, not
            # UTC -- branch names are read by humans.
            s["timestamp"] = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

            # Scoped to the pipeline run itself (not the back-and-forth
            # spent resolving the repo) -- that's what "duration/tokens of
            # the analysis" in the final report actually means.
            s[sk.RUN_START_TIME] = time.time()
            s[sk.TOKEN_USAGE] = {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}

            try:
                async for event in pipeline.run_async(ctx):
                    _accumulate_tokens(s, event)
                    yield event
            except (
                ToolNotAvailableError, BuildToolNotDetectedError,
                SonarConfigNotFoundError, SonarPreflightError,
                # RuntimeError / TimeoutError: the pipeline's own "stop and
                # tell the user why" signal for failures discovered mid-run,
                # not just at preflight -- a Sonar scan that never printed a
                # CE task id, a checkpoint whose full build still fails after
                # reverting the whole batch, a background analysis task that
                # times out or comes back FAILED/CANCELED, `git push` with no
                # origin configured. Every one of those sites raises
                # RuntimeError or TimeoutError specifically so a human sees
                # why -- confirmed live these were NOT being caught here,
                # so instead of "Analysis stopped: <reason>" the entire run
                # died with no message reaching the chat at all (adk web
                # surfaced it only as a bare "execution failed", the actual
                # reason visible nowhere). Deliberately not bare Exception:
                # that would also swallow real bugs (NameError, etc.) behind
                # the same generic message instead of surfacing them.
                RuntimeError, TimeoutError,
            ) as e:
                # SetupStep's preflight checks all deliberately raise before
                # any issue fetch or LLM call and are NOT caught inside the
                # pipeline -- this is the one place that turns that into a
                # clean chat message instead of an unhandled exception
                # reaching adk web's request handler.
                yield _model_msg(self.name, f"Analysis stopped: {e}")

    return _IntakeStep(sub_agents=[pipeline])

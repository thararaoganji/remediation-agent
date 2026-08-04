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
skips the conversation and falls straight through to pipeline_agent in the
same turn — mirrors the manual sub-agent dispatch CheckpointGate uses for
checkpoint_pipeline in agents.py, since a plain SequentialAgent can't
conditionally skip a sub-agent.
"""

import datetime
import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.tools import ToolContext
from google.genai import types

from . import state_schema as sk

REQUIRED_ENV = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "SONAR_PROJECT_KEY", "LANGUAGE"]

INTAKE_INSTRUCTION = """
You are the intake step for the Sonar Auto-Fix Agent. Your ONLY job is to
collect one piece of information from the user: which repository to run a
Sonar analysis against — either a local filesystem path, or a GitHub repo
("owner/repo" or a full GitHub URL).

Once you have a clear answer, call `set_analysis_source` with:
- source_type: "local" or "github"
- source: the path (if local) or "owner/repo"/URL (if github)

Strict scope — this is not a general-purpose assistant:
- Never answer coding questions, explain code, write or fix code yourself,
  or discuss anything unrelated to starting a Sonar analysis run.
- Never take any action other than calling `set_analysis_source` once you
  have a valid local path or GitHub repo identifier.
- If asked to do anything else, politely decline and steer the
  conversation back to asking for the repo to analyze. For example: "I can
  only help start a Sonar analysis — could you share the local path or
  GitHub repo you'd like analyzed?"
- If the request is ambiguous (e.g. just a bare name with no indication of
  local vs GitHub), ask a brief clarifying question instead of guessing.
- If `set_analysis_source` returns an error, relay the problem plainly and
  ask for a corrected path or repo.
- Always be polite, warm, and concise.
"""


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
    model="gemini-2.5-flash",
    instruction=INTAKE_INSTRUCTION,
    tools=[set_analysis_source],
)


class IntakeStep(BaseAgent):
    """Gate + state-seeding step. See module docstring."""

    name: str = "intake_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state

        if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
            async for event in intake_llm_agent.run_async(ctx):
                yield event
            if not (s.get("source") and s.get(sk.SOURCE_TYPE)):
                # The LLM asked a clarifying question or declined an
                # out-of-scope request instead of calling the tool. End this
                # turn here and wait for the next user message.
                yield Event(author=self.name, actions=EventActions(escalate=True))
                return

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
            yield Event(author=self.name, actions=EventActions(escalate=True))
            return

        s.setdefault(sk.SONAR_PROJECT_KEY, os.environ["SONAR_PROJECT_KEY"])
        s.setdefault(sk.LANGUAGE, os.environ["LANGUAGE"])
        s.setdefault("sonar_base_url", os.environ["SONAR_BASE_URL"])
        s.setdefault("sonar_token", os.environ["SONAR_TOKEN"])
        s.setdefault("ce_edition", os.environ.get("CE_EDITION", "true").lower() == "true")
        s.setdefault("github_token", os.environ.get("GITHUB_TOKEN") or None)
        s.setdefault("workspace_root", os.environ.get("WORKSPACE_ROOT", "/tmp/sonar_autofix_workspaces"))
        s.setdefault("timestamp", datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))

        yield Event(author=self.name, content=None)

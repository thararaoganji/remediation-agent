"""
Shared conversational-intake plumbing for all three Sonar agents. Each
agent (autofix/coverage/duplicate) keeps its own thin IntakeStep (its
welcome message, its own pipeline to hand off to once source/source_type
are known) but all three share this module's REQUIRED_ENV check,
set_analysis_source tool, intake_llm_agent, scope-redirect message, and
token-accounting helper -- see sonar/techdebt_agent/intake.py for the
fullest-featured example of how a per-agent IntakeStep uses these.
"""

import os

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.tools import ToolContext

from core import state_schema as sk

REQUIRED_ENV = ["GOOGLE_API_KEY", "SONAR_BASE_URL", "SONAR_TOKEN", "LANGUAGE"]

_BRANCH_HINT = (
    "\n\nBy default I use the repo's default branch. If you want a "
    "different one, just say so (e.g. \"the develop branch\" or "
    "\"release/v2\")."
)

WELCOME_MESSAGE = """\
I'm the Sonar Auto-Fix Agent. I fix SonarQube findings in a Java project \
— local or GitHub — file by file, verifying the build and re-scanning \
after each batch so nothing regresses.

Which repo would you like me to analyze — a local path, or the full GitHub \
repo URL (the repo whose root contains build.gradle or pom.xml)?""" + _BRANCH_HINT

DUPLICATE_WELCOME_MESSAGE = """\
I'm the Sonar Duplicate-Fix Agent. I detect and resolve code duplication \
in your Java project — local or GitHub — by extracting shared logic into clean helpers.

Which repo would you like me to analyze — a local path, or the full GitHub \
repo URL (the repo whose root contains build.gradle or pom.xml)?""" + _BRANCH_HINT

COVERAGE_WELCOME_MESSAGE = """\
I'm the Sonar Coverage-Enhance Agent. I find uncovered code paths/branches \
and automatically generate unit tests (JUnit) to boost test coverage.

Which repo would you like me to analyze — a local path, or the full GitHub \
repo URL (the repo whose root contains build.gradle or pom.xml)?""" + _BRANCH_HINT

# Shown verbatim, every time, whenever the user's message doesn't resolve
# to a valid repo — see each agent's IntakeStep. Fixed and deterministic
# rather than whatever intake_llm_agent might phrase, so the scope
# restriction and "politely decline" behavior are guaranteed, not just
# instructed and hoped-for.
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


def _accumulate_tokens(state: dict, event: Event) -> None:
    """Event extends google-adk's LlmResponse, so every event carries a
    usage_metadata field — populated only on events that actually came
    back from a model call (fix_llm_agent, one per file, is the dominant
    cost here), None on every deterministic BaseAgent step's own events.
    Each agent's IntakeStep re-yields every event its pipeline produces,
    including ones from agents nested arbitrarily deep in outer/per-file/
    checkpoint loops, so hooking this one spot sees the whole run."""
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

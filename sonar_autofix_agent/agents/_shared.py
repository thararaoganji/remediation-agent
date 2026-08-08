"""
The one helper genuinely needed by every phase submodule in this package
-- kept in its own tiny file (rather than defined in __init__.py) so
submodules can import it without risking a circular import back through
__init__.py, which itself imports from all of them to re-export/assemble
pipeline_agent.
"""

from google.genai import types


def _msg(text: str) -> types.Content:
    """Every custom BaseAgent step in this package was originally silent
    (content=None) — deterministic orchestration doesn't need an LLM to
    narrate it, so there's no model turn to show. But that leaves adk
    web's event list full of unlabeled placeholder entries with nothing
    to click into. This wraps a short, fixed status line as the event's
    content instead — still not LLM-generated, just a visible echo of
    the decision the step already made."""
    return types.Content(role="model", parts=[types.Part(text=text)])

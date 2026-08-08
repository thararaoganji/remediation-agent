"""
Thin re-export so `adk web`'s directory-based discovery finds this package.

adk web only recognizes a folder as an agent directory if it contains
agent.py or root_agent.yaml directly (see google.adk.cli.utils.agent_loader
.is_single_agent_directory) — a bare __init__.py exposing root_agent isn't
enough, even though `adk run` and manual imports load it fine either way.
The real composition (intake front door + pipeline_agent) lives in
__init__.py; this file just makes it visible to adk web's discovery.

Not to be confused with the sibling `agents/` package (plural) — that's
where pipeline_agent and every BaseAgent step class actually live. This
file's name is fixed by adk web's own discovery convention above; it
can't be renamed to something less similar without breaking that.
"""

from . import root_agent

__all__ = ["root_agent"]

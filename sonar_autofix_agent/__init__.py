from google.adk.agents import SequentialAgent

from .agents import pipeline_agent
from .intake import IntakeStep

# Conversational front door (IntakeStep) wraps the deterministic pipeline.
# IntakeStep no-ops straight through to pipeline_agent when source/source_type
# are already seeded in session.state (e.g. run_local.py's .env-driven path),
# so this same root_agent serves both the automated and conversational entry
# points. See intake.py and agents.py's module docstrings.
root_agent = SequentialAgent(
    name="sonar_autofix_root",
    sub_agents=[IntakeStep(), pipeline_agent],
)

__all__ = ["root_agent"]

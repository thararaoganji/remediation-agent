"""Composes the deterministic Sonar remediation graph for the auto-fix
agent. Kept as its own module (not defined directly in __init__.py) so
intake.py and __init__.py can both import `pipeline_agent` from here
without a circular import -- intake.py needs it to invoke once source/
source_type are known, and __init__.py needs it to build root_agent."""

from google.adk.agents import SequentialAgent

from core.agents.report import PushStep

from sonar.setup import SetupStep
from .maintainability import maintainability_expansion_loop
from .outer_loop import outer_loop
from .report import ReportStep

pipeline_agent = SequentialAgent(
    name="sonar_autofix_pipeline",
    sub_agents=[SetupStep(), outer_loop, maintainability_expansion_loop, PushStep(), ReportStep()],
)

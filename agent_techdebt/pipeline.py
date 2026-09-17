"""Composes the deterministic Sonar remediation graph for the Tech-Debt
Agent. Kept as its own module rather than inline in __init__.py so the
graph definition stays separate from the root-agent wrapper: __init__.py
imports `pipeline_agent` from here and passes it to build_intake_step()."""

from google.adk.agents import SequentialAgent

from core.agents.report import PushStep

from sonar.setup import SetupStep
from .maintainability import maintainability_expansion_loop
from .outer_loop import outer_loop
from .report import ReportStep

pipeline_agent = SequentialAgent(
    name="sonar_techdebt_pipeline",
    sub_agents=[SetupStep(), outer_loop, maintainability_expansion_loop, PushStep(), ReportStep()],
)

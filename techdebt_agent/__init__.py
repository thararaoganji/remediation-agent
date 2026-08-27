from .intake import IntakeStep
from .pipeline import pipeline_agent

# IntakeStep IS root_agent directly -- not wrapped in a SequentialAgent
# alongside pipeline_agent. See intake.py's module docstring for why.
# sub_agents=[pipeline_agent] is declared here purely for introspection
# (adk web's graph view, agent_loader language detection, etc.) -- it has
# no effect on runtime control flow: IntakeStep's manual
# pipeline_agent.run_async(ctx) call is what actually decides whether and
# when pipeline_agent runs.
root_agent = IntakeStep(sub_agents=[pipeline_agent])

__all__ = ["root_agent", "pipeline_agent"]

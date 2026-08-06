from .agents import pipeline_agent
from .intake import IntakeStep

# IntakeStep IS root_agent directly — not wrapped in a SequentialAgent
# alongside pipeline_agent. SequentialAgent in this ADK version doesn't
# stop early on a sub-agent's escalate=True (that's LoopAgent-only), so a
# wrapper couldn't actually gate pipeline_agent from running before the
# repo location was collected. IntakeStep invokes pipeline_agent itself,
# manually, only once source/source_type are in state — see intake.py's
# module docstring. This same root_agent serves both the automated
# .env-driven path (run_local.py) and the conversational one (adk run/web),
# since IntakeStep skips the LLM step whenever source/source_type are
# already pre-seeded.
#
# sub_agents=[pipeline_agent] is declared here purely for introspection
# (adk web's graph view, agent_loader language detection, etc.) — those
# walk agent.sub_agents statically and don't execute anything themselves.
# It has no effect on runtime control flow: IntakeStep's manual
# pipeline_agent.run_async(ctx) call is what actually decides whether and
# when pipeline_agent runs. Without this, pipeline_agent — and everything
# under it (SetupStep, outer_loop, etc.) — is invisible to the graph view,
# since it's never reached by walking sub_agents otherwise.
root_agent = IntakeStep(sub_agents=[pipeline_agent])

__all__ = ["root_agent"]

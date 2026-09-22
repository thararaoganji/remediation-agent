"""Sonar Tech-Debt Agent -- fixes SonarQube Security, Reliability and
Maintainability findings in a Java project file by file, verifies the
build, and re-scans to confirm no regressions, targeting ratings of A."""

from sonar.intake import BRANCH_HINT, REPO_PROMPT, build_intake_step

from .pipeline import pipeline_agent

AGENT_SLUG = "techdebt"

WELCOME_MESSAGE = (
    "I'm the Sonar Tech-Debt Agent. I fix SonarQube Security, Reliability "
    "and Maintainability findings in a Java project on GitHub — "
    "file by file, verifying the build and re-scanning after each batch so "
    "nothing regresses.\n\n" + REPO_PROMPT + BRANCH_HINT
)

# The intake step IS root_agent directly -- not wrapped in a
# SequentialAgent alongside pipeline_agent. See sonar/intake.py's module
# docstring for why. sub_agents=[pipeline_agent] is declared inside
# build_intake_step purely for introspection (adk web's graph view,
# agent_loader language detection); it has no effect on runtime control flow.
root_agent = build_intake_step(
    step_name="techdebt_intake_step",
    description=(
        "Fetches SonarQube findings for a Java project on GitHub, "
        "fixes them file-by-file, verifies the build, and re-scans to "
        "confirm no regressions -- targeting Security/Reliability/"
        "Maintainability ratings of A. Send any message to begin."
    ),
    welcome_message=WELCOME_MESSAGE,
    start_phrase="Starting the Sonar analysis now.",
    pipeline=pipeline_agent,
    agent_slug=AGENT_SLUG,
)

__all__ = ["root_agent", "pipeline_agent", "AGENT_SLUG"]

"""Sonar Coverage Agent -- finds uncovered code paths/branches in a Java
project and generates targeted JUnit tests to raise test coverage,
verifying the build and re-scanning after each batch."""

from sonar.intake import BRANCH_HINT, REPO_PROMPT, build_intake_step

from .enhance_coverage import coverage_pipeline

AGENT_SLUG = "coverage"

WELCOME_MESSAGE = (
    "I'm the Sonar Coverage Agent. I find uncovered code paths/branches "
    "and automatically generate unit tests (JUnit) to boost test "
    "coverage.\n\n" + REPO_PROMPT + BRANCH_HINT
)

root_agent = build_intake_step(
    step_name="coverage_intake_step",
    description=(
        "Finds uncovered code paths/branches in a Java project (local or GitHub), "
        "and automatically generates targeted JUnit unit tests to boost test coverage. "
        "Send any message to begin."
    ),
    welcome_message=WELCOME_MESSAGE,
    start_phrase="Starting the coverage enhancement now.",
    pipeline=coverage_pipeline,
    agent_slug=AGENT_SLUG,
)

__all__ = ["root_agent", "coverage_pipeline", "AGENT_SLUG"]

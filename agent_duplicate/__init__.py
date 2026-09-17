"""Sonar Duplication Agent -- detects duplicated code blocks in a Java
project and refactors the shared logic into clean, reusable helpers,
verifying the build and re-scanning after each batch."""

from sonar.intake import BRANCH_HINT, REPO_PROMPT, build_intake_step

from .fix_duplicate import duplicate_pipeline

AGENT_SLUG = "duplicate"

WELCOME_MESSAGE = (
    "I'm the Sonar Duplication Agent. I detect and resolve code "
    "duplication in your Java project — local or GitHub — by extracting "
    "shared logic into clean helpers.\n\n" + REPO_PROMPT + BRANCH_HINT
)

root_agent = build_intake_step(
    step_name="duplicate_intake_step",
    description=(
        "Detects code duplication in a Java project (local or GitHub), "
        "and automatically extracts shared logic into clean, reusable "
        "helper classes/methods. Send any message to begin."
    ),
    welcome_message=WELCOME_MESSAGE,
    start_phrase="Starting the duplicate logic extraction now.",
    pipeline=duplicate_pipeline,
    agent_slug=AGENT_SLUG,
)

__all__ = ["root_agent", "duplicate_pipeline", "AGENT_SLUG"]

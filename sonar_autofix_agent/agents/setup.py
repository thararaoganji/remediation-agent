"""Phase I — Setup (Section 3)."""

import os
import tempfile
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from .. import state_schema as sk
from ..adapters.base import get_adapter
from ..tools import git_tools, sonar_tools
from ._shared import _msg


class SetupStep(BaseAgent):
    name: str = "setup_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        working_dir = git_tools.resolve_source(
            s["source"],
            s[sk.SOURCE_TYPE],
            workspace_root=s.get(
                "workspace_root", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
            ),
            github_token=s.get("github_token"),
        )
        # Fail fast, before any Sonar fetch or LLM call: resolve the actual
        # build tool (auto-detected for a generic "java" LANGUAGE, or the
        # explicit override from .env) and confirm the required binaries
        # are on PATH. ToolNotAvailableError / BuildToolNotDetectedError are
        # deliberately NOT caught here — they propagate out of SetupStep and
        # stop the whole run immediately, with a clear actionable message,
        # rather than failing confusingly deep inside the per-file loop on
        # the first quick_compile_check().
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        adapter.preflight_check(working_dir)
        s["temp:resolved_language"] = type(adapter).__name__

        # Read from the build file, not .env: the project key the Sonar
        # plugin actually uses when run_sonar_scan() invokes `gradle sonar`
        # / `mvn sonar:sonar` is whatever's configured in build.gradle/
        # pom.xml — a mismatched .env value would fetch/report against one
        # project key while the scan itself analyzes under another.
        s[sk.SONAR_PROJECT_KEY] = adapter.get_project_key(working_dir)

        # SonarPreflightError deliberately NOT caught here — same "fail
        # fast before any branch is created or issue fetched" contract as
        # the tool/build-file checks above. A project key that resolves
        # cleanly from the build file can still be one that's never been
        # scanned on this server (or was scanned under a different key) —
        # without this, the run would proceed to create a branch and then
        # silently find 0 issues, with nothing telling the user why.
        sonar_tools.validate_connection(s["sonar_base_url"], s["sonar_token"])
        sonar_tools.check_project_analyzed(s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"])

        branch_name = git_tools.create_branch(
            working_dir, s[sk.SONAR_PROJECT_KEY], s.get("timestamp", "")
        )
        s[sk.WORKING_DIR] = working_dir
        s[sk.BRANCH_NAME] = branch_name

        s.setdefault(sk.OUTER_ITERATION, 0)
        s.setdefault(sk.MAX_OUTER_ITERATIONS, 5)
        s.setdefault(sk.CHECKPOINT_BATCH_SIZE, 8)
        s.setdefault(sk.FILES_SINCE_CHECKPOINT, 0)
        s.setdefault(sk.FILES_COMPLETED, [])
        s.setdefault(sk.FILES_FLAGGED, [])
        s.setdefault(sk.FILES_REVERTED_AT_CHECKPOINT, [])
        s.setdefault(sk.ISSUES_FIXED, [])
        s.setdefault(sk.ISSUES_NO_SAFE_FIX, [])
        s.setdefault(sk.CHECKPOINTS, [])
        s.setdefault(sk.WONT_FIX_REVIEW_QUEUE, [])
        s.setdefault(sk.MAINTAINABILITY_EXPANSION_ITERATION, 0)
        s.setdefault(sk.MAINTAINABILITY_EXPANSION_BATCH_SIZE, 8)
        yield Event(author=self.name, content=_msg(f"Checked out branch `{branch_name}`. Fetching Sonar issues next."))

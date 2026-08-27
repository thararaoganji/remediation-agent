"""Setup step shared by all three Sonar agents (autofix, coverage,
duplicate): resolves the source, validates the Sonar connection/project
key, creates the run's branch, and seeds default state. Sonar-specific
(a Veracode/Black Duck setup step would validate against a different API
entirely), so it lives here rather than in core -- but shared across
Sonar's own agents rather than duplicated three times."""

import os
import tempfile
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from core import state_schema as sk
from core.agents._shared import _msg
from core.tools import git_tools

from .adapters import SonarPreflightError, get_adapter
from .tools import sonar_tools


class SetupStep(BaseAgent):
    name: str = "setup_step"

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        s = ctx.session.state
        source_branch = s.get("source_branch")
        working_dir = git_tools.resolve_source(
            s["source"],
            s[sk.SOURCE_TYPE],
            workspace_root=s.get(
                "workspace_root", os.path.join(tempfile.gettempdir(), "sonar_autofix_workspaces")
            ),
            github_token=s.get("github_token"),
            source_branch=source_branch,
        )
        # Fail fast, before any Sonar fetch or LLM call: resolve the actual
        # build tool (auto-detected for a generic "java" LANGUAGE, or the
        # explicit override from .env) and confirm the required binaries
        # are on PATH. ToolNotAvailableError / BuildToolNotDetectedError are
        # deliberately NOT caught here -- they propagate out of SetupStep and
        # stop the whole run immediately, with a clear actionable message,
        # rather than failing confusingly deep inside the per-file loop on
        # the first quick_compile_check().
        adapter = get_adapter(s[sk.LANGUAGE], working_dir)
        adapter.preflight_check(working_dir)
        s["temp:resolved_language"] = type(adapter).__name__

        # Read from the build file, not .env: the project key the Sonar
        # plugin actually uses when run_sonar_scan() invokes `gradle sonar`
        # / `mvn sonar:sonar` is whatever's configured in build.gradle/
        # pom.xml -- a mismatched .env value would fetch/report against one
        # project key while the scan itself analyzes under another.
        s[sk.SONAR_PROJECT_KEY] = adapter.get_project_key(working_dir)

        # SonarPreflightError deliberately NOT caught here -- same "fail
        # fast before any branch is created or issue fetched" contract as
        # the tool/build-file checks above. A project key that resolves
        # cleanly from the build file can still be one that's never been
        # scanned on this server (or was scanned under a different key) --
        # without this, the run would proceed to create a branch and then
        # silently find 0 issues, with nothing telling the user why.
        sonar_tools.validate_connection(s["sonar_base_url"], s["sonar_token"])
        sonar_tools.check_project_analyzed(s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], s["sonar_token"])

        # A specific source_branch changes which code got checked out
        # (git_tools.resolve_source above), so it must also change which
        # branch's Sonar analysis FetchPrioritizeStep/CoverageFetchStep/
        # DuplicateFetchStep pull issues from (see each's branch=... arg) --
        # otherwise the agent would fix issues found on a DIFFERENT branch's
        # code than what's actually checked out, which can easily mismatch
        # on line numbers or even which files exist. Checked here, fast,
        # rather than letting the fetch step silently return zero issues
        # for a branch that was never analyzed.
        if source_branch and not sonar_tools.branch_exists(
            s["sonar_base_url"], s[sk.SONAR_PROJECT_KEY], source_branch, s["sonar_token"]
        ):
            raise SonarPreflightError(
                f"Branch '{source_branch}' has no analysis of its own on this Sonar server "
                f"({s['sonar_base_url']}) yet. Run a scan against that branch first -- e.g. "
                f"`./mvnw sonar:sonar -Dsonar.projectKey={s[sk.SONAR_PROJECT_KEY]} "
                f"-Dsonar.branch.name={source_branch} -Dsonar.host.url={s['sonar_base_url']} "
                f"-Dsonar.token=<token>` (Maven) or the equivalent `./gradlew sonar` (Gradle) -- "
                "then re-run."
            )

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
        source_note = f" (based on `{source_branch}`)" if source_branch else ""
        yield Event(author=self.name, content=_msg(
            f"Checked out branch `{branch_name}`{source_note}. Fetching Sonar issues next."
        ))

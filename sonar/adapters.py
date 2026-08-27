"""
Sonar-specific adapter behavior: resolving the Sonar project key from a
project's own build file, and invoking that build tool's Sonar plugin.

Both are genuinely Sonar-specific -- a Veracode/Black Duck integration has
its own, different way to identify a project and trigger a scan/upload --
so they're layered on top of core.adapters.base's plain build-tool
wrappers via subclassing rather than living on the shared LanguageAdapter
interface itself. Everything else (compile/verify/test) is inherited
unchanged from core.
"""

import os
import re
import xml.etree.ElementTree as ET

from core.adapters.base import (  # noqa: F401 -- re-exported for convenience
    ADAPTER_REGISTRY as _CORE_ADAPTER_REGISTRY,
    BuildResult, BuildToolNotDetectedError, JavaGradleAdapter, JavaMavenAdapter,
    ToolNotAvailableError, detect_build_tool,
)
from core.adapters.base import _run, _combined_output  # noqa: F401 -- reused by run_sonar_scan


class SonarConfigNotFoundError(Exception):
    """Raised by get_project_key() when the checked-out repo's own build
    file has no usable Sonar project key configured (no sonar.projectKey
    property, and — for Maven — no groupId/artifactId to fall back to).
    Deliberately NOT caught in this package's agents — same "fail fast
    before any Sonar fetch or LLM call" contract as the tool/build-file
    checks in core.adapters.base, propagated out of setup and surfaced as
    a clean stop by both run_local.py and the chat intake path."""
    pass


class SonarPreflightError(Exception):
    """Raised by sonar_tools.validate_connection()/check_project_analyzed()
    when the Sonar server itself isn't in a usable state for this run --
    unreachable, token rejected, or the resolved project key has never
    actually been analyzed there. Same "fail fast before any branch is
    created or issue fetched" contract as the other preflight exceptions."""
    pass


_CE_TASK_ID_RE = re.compile(r"api/ce/task\?id=([\w-]+)")


def _parse_ce_task_id(output: str) -> str:
    """Every Sonar scanner integration (Gradle/Maven plugins, standalone
    CLI) prints an identical 'More about the report processing at
    .../api/ce/task?id=<id>' line on success -- this format is stable
    across scanner versions and is the documented way to get the
    background task id for polling."""
    m = _CE_TASK_ID_RE.search(output)
    if not m:
        raise RuntimeError(
            "Sonar scan finished but no Compute Engine task id found in its "
            f"output -- can't poll for processing status. Last output:\n{output[-2000:]}"
        )
    return m.group(1)


class SonarJavaMavenAdapter(JavaMavenAdapter):
    def get_project_key(self, working_dir: str) -> str:
        """Reads the Sonar project key straight from pom.xml (the
        <sonar.projectKey> property, or the groupId:artifactId default the
        Sonar Maven plugin falls back to) rather than trusting a value
        passed in from outside -- the project key actually configured in
        the build is what run_sonar_scan() will use regardless of what's
        in .env. Raises SonarConfigNotFoundError with an actionable
        message if it can't be found."""
        pom_path = os.path.join(working_dir, "pom.xml")
        if not os.path.isfile(pom_path):
            raise SonarConfigNotFoundError(
                f"No Sonar configuration found — {pom_path} does not exist. "
                f"Stopping before any Sonar fetch or fix generation."
            )
        try:
            tree = ET.parse(pom_path)
        except ET.ParseError as e:
            raise SonarConfigNotFoundError(
                f"No Sonar configuration found — {pom_path} is not valid XML ({e}). "
                f"Stopping before any Sonar fetch or fix generation."
            )

        root = tree.getroot()
        # pom.xml's default namespace makes every findtext() need the ns
        # prefix explicitly, or ElementTree silently returns None.
        ns = {"m": "http://maven.apache.org/POM/4.0.0"} if root.tag.startswith("{") else {}

        def find(path: str) -> str | None:
            tag = "/".join(f"m:{seg}" for seg in path.split("/")) if ns else path
            return root.findtext(tag, namespaces=ns)

        explicit = find("properties/sonar.projectKey")
        if explicit:
            return explicit.strip()

        # Sonar's Maven plugin defaults the project key to groupId:artifactId
        # when sonar.projectKey isn't set explicitly -- groupId is frequently
        # only on the <parent> block, so fall back there.
        group_id = find("groupId") or find("parent/groupId")
        artifact_id = find("artifactId")
        if group_id and artifact_id:
            return f"{group_id.strip()}:{artifact_id.strip()}"

        raise SonarConfigNotFoundError(
            f"No Sonar configuration found in {pom_path} — no <sonar.projectKey> "
            f"property and no resolvable groupId/artifactId. Add "
            f"<sonar.projectKey>...</sonar.projectKey> under <properties> in pom.xml. "
            f"Stopping before any Sonar fetch or fix generation."
        )

    def run_sonar_scan(self, working_dir, sonar_base_url, sonar_token, project_key, branch=None):
        mvn = self._mvn_cmd(working_dir)
        # Fully-qualified plugin goal, not the "sonar:sonar" prefix shorthand.
        # Prefix resolution only works if org.sonarsource.scanner.maven is
        # already registered in this Maven install's ~/.m2/settings.xml
        # <pluginGroups>, or the target project's own pom.xml already
        # declares sonar-maven-plugin as a build plugin (which registers the
        # prefix from the reactor itself). Neither holds for a repo that's
        # never had Sonar wired into its POM. The fully-qualified
        # groupId:artifactId:goal form resolves directly from the repository
        # and doesn't depend on either.
        args = [
            mvn, "org.sonarsource.scanner.maven:sonar-maven-plugin:sonar",
            f"-Dsonar.host.url={sonar_base_url}", f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.token={sonar_token}",
        ]
        if branch:
            args.append(f"-Dsonar.branch.name={branch}")
        result = _run(args, cwd=working_dir, timeout=900)
        if result.returncode != 0:
            raise RuntimeError(
                f"Sonar scan failed ({mvn} org.sonarsource.scanner.maven:sonar-maven-plugin:sonar):\n"
                f"{_combined_output(result)[-3000:]}"
            )
        return _parse_ce_task_id(result.stdout + result.stderr)


class SonarJavaGradleAdapter(JavaGradleAdapter):
    # Groovy: property "sonar.projectKey", "value"  |  Kotlin: property("sonar.projectKey", "value")
    _PROJECT_KEY_PROPERTY_RE = re.compile(
        r'property\(?\s*["\']sonar\.projectKey["\']\s*,\s*["\']([^"\']+)["\']'
    )
    # gradle.properties: systemProp.sonar.projectKey=value  |  sonar.projectKey=value
    _PROJECT_KEY_PROPS_FILE_RE = re.compile(
        r'^(?:systemProp\.)?sonar\.projectKey\s*=\s*(\S+)\s*$', re.MULTILINE
    )

    def get_project_key(self, working_dir: str) -> str:
        build_files_found = False
        for build_file in ("build.gradle", "build.gradle.kts"):
            path = os.path.join(working_dir, build_file)
            if not os.path.isfile(path):
                continue
            build_files_found = True
            with open(path, encoding="utf-8") as f:
                m = self._PROJECT_KEY_PROPERTY_RE.search(f.read())
            if m:
                return m.group(1)

        props_path = os.path.join(working_dir, "gradle.properties")
        if os.path.isfile(props_path):
            with open(props_path, encoding="utf-8") as f:
                m = self._PROJECT_KEY_PROPS_FILE_RE.search(f.read())
            if m:
                return m.group(1)

        if not build_files_found:
            raise SonarConfigNotFoundError(
                f"No Sonar configuration found — no build.gradle or build.gradle.kts "
                f"in {working_dir}. Stopping before any Sonar fetch or fix generation."
            )
        raise SonarConfigNotFoundError(
            f"No Sonar configuration found in build.gradle[.kts] or gradle.properties "
            f"in {working_dir} — no sonar.projectKey property set. Add "
            f"`property \"sonar.projectKey\", \"...\"` inside the sonar {{ properties {{ ... }} }} "
            f"block in build.gradle. Stopping before any Sonar fetch or fix generation."
        )

    def run_sonar_scan(self, working_dir, sonar_base_url, sonar_token, project_key, branch=None):
        gradle = self._gradle_cmd(working_dir)
        # --info is required, not cosmetic: this plugin logs its own
        # "ANALYSIS SUCCESSFUL ... More about the report processing at
        # .../api/ce/task?id=..." line at INFO level through its embedded
        # SLF4J logger, which Gradle's default LIFECYCLE log threshold
        # suppresses -- verified live, the line is simply absent without it.
        #
        # -Dsonar.token= (not the SONAR_TOKEN env var): verified live that
        # a project's own hardcoded `property "sonar.token", "..."` in the
        # sonar{} DSL block silently wins over SONAR_TOKEN -- an invalid
        # env var still authenticated successfully. -D properties DO
        # correctly override the DSL block, so that's the only reliable
        # way to guarantee .env's token is what's actually used.
        args = [
            gradle, "sonar", "--info",
            f"-Dsonar.host.url={sonar_base_url}", f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.token={sonar_token}",
        ]
        if branch:
            args.append(f"-Dsonar.branch.name={branch}")
        result = _run(args, cwd=working_dir, timeout=900)
        if result.returncode != 0:
            raise RuntimeError(f"Sonar scan failed (gradle sonar):\n{_combined_output(result)[-3000:]}")
        return _parse_ce_task_id(result.stdout + result.stderr)


ADAPTER_REGISTRY = {
    "java-maven": SonarJavaMavenAdapter,
    "java-gradle": SonarJavaGradleAdapter,
}


def get_adapter(language: str, working_dir: str | None = None):
    """Same resolution rule as core.adapters.base.get_adapter (language ==
    'java' triggers auto-detection against working_dir), but returns this
    module's Sonar-flavored adapter subclasses -- the only ones with
    get_project_key()/run_sonar_scan()."""
    resolved = language
    if language == "java":
        if working_dir is None:
            raise ValueError("working_dir is required to auto-detect a 'java' project's build tool")
        resolved = detect_build_tool(working_dir)

    if resolved not in ADAPTER_REGISTRY:
        raise ValueError(f"No LanguageAdapter registered for '{resolved}'")
    return ADAPTER_REGISTRY[resolved]()

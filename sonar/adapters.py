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
import tempfile
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


_GRADLE_JACOCO_XML_INIT = """\
// Injected by the Sonar agents for one scan only (never written into the
// repo). Two jobs:
//  1. Gradle's JaCoCo plugin leaves XML output OFF by default, and the XML
//     report is exactly what the `sonar` task needs to read coverage.
//  2. The org.sonarqube plugin does NOT run jacocoTestReport itself, so when
//     `test jacocoTestReport sonar` are requested together, force `sonar` to
//     run after the report so it sees fresh coverage in the same build.
allprojects {
    plugins.withId('jacoco') {
        tasks.matching { it.name == 'jacocoTestReport' }.configureEach {
            reports { xml.required = true }
        }
    }
    tasks.matching { it.name == 'sonar' || it.name == 'sonarqube' }.configureEach {
        mustRunAfter(tasks.matching { it.name == 'jacocoTestReport' })
    }
}
"""

_GRADLE_JACOCO_XML_PATH = "build/reports/jacoco/test/jacocoTestReport.xml"
_MAVEN_JACOCO_XML_PATH = "target/site/jacoco/jacoco.xml"


def _write_gradle_jacoco_init_script(working_dir: str) -> str:
    """Writes the init script to a STABLE path (derived from the clone dir),
    not a random tempfile name. Gradle keys daemon compatibility partly on
    the set of init-script paths -- a fresh random name every scan defeats
    daemon reuse and, combined with the agent's many rapid Gradle
    invocations, trips 'daemon context mismatch' failures."""
    path = os.path.join(
        tempfile.gettempdir(), f"sonar-agent-jacoco-{os.path.basename(os.path.normpath(working_dir))}.init.gradle"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(_GRADLE_JACOCO_XML_INIT)
    return path


def project_has_coverage_tooling(working_dir: str) -> bool:
    """True if this project's build is configured to produce a code-coverage
    report a Sonar scan can consume (JaCoCo for Java). The coverage agent
    preflights on this: without it every file reads 0% coverage on Sonar and
    the tests it generates have no measurable effect."""
    try:
        build_tool = detect_build_tool(working_dir)
    except BuildToolNotDetectedError:
        return False
    if build_tool == "java-maven":
        with open(os.path.join(working_dir, "pom.xml"), encoding="utf-8") as f:
            return "jacoco-maven-plugin" in f.read()
    for name in ("build.gradle", "build.gradle.kts", "gradle.properties"):
        path = os.path.join(working_dir, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                if re.search(r"jacoco", f.read(), re.IGNORECASE):
                    return True
    return False


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

    def run_sonar_scan(self, working_dir, sonar_base_url, sonar_token, project_key, branch=None, with_coverage=False):
        mvn = self._mvn_cmd(working_dir)
        # Strictly gated on the caller's flag -- NOT auto-detected from the
        # project, even though the project can tell us. Confirmed live: a
        # tech-debt run against a repo the coverage agent had already added
        # JaCoCo to picked up this extra `test jacoco:report` work it never
        # asked for and doesn't need, on every single checkpoint scan --
        # pure added failure surface (an unrelated test flake, a slower
        # scan) for an agent that was never going to read the coverage
        # number. Only the coverage agent sets with_coverage=True.
        jacoco = with_coverage

        # Fully-qualified plugin goals, not the "sonar:sonar" prefix shorthand.
        # Prefix resolution only works if org.sonarsource.scanner.maven is
        # already registered in this Maven install's ~/.m2/settings.xml
        # <pluginGroups>, or the target project's own pom.xml already declares
        # the plugin. Neither holds for a repo that's never had Sonar wired in.
        goals = []
        if jacoco:
            goals += ["test", "org.jacoco:jacoco-maven-plugin:report"]
        goals.append("org.sonarsource.scanner.maven:sonar-maven-plugin:sonar")

        args = [
            mvn, *goals,
            f"-Dsonar.host.url={sonar_base_url}", f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.token={sonar_token}",
        ]
        if jacoco:
            # -Dmaven.test.failure.ignore so one flaky pre-existing test can't
            # block the scan; the explicit path removes any reliance on
            # sonar-maven's own auto-detection.
            args += [
                "-Dmaven.test.failure.ignore=true",
                f"-Dsonar.coverage.jacoco.xmlReportPaths={_MAVEN_JACOCO_XML_PATH}",
            ]
        if branch:
            args.append(f"-Dsonar.branch.name={branch}")
        result = _run(args, cwd=working_dir, timeout=1200)
        # The scan itself succeeded if the CE task URL is in the output -- true
        # even when -Dmaven.test.failure.ignore produced a non-zero exit from
        # an unrelated failing test.
        try:
            return _parse_ce_task_id(result.stdout + result.stderr)
        except RuntimeError:
            raise RuntimeError(
                f"Sonar scan failed ({mvn} {' '.join(goals)}):\n{_combined_output(result)[-3000:]}"
            )


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

    def run_sonar_scan(self, working_dir, sonar_base_url, sonar_token, project_key, branch=None, with_coverage=False):
        gradle = self._gradle_cmd(working_dir)
        # Strictly gated on the caller's flag -- NOT auto-detected from the
        # project, even though the project can tell us. Confirmed live: a
        # tech-debt run against a repo the coverage agent had already added
        # JaCoCo to picked up this extra `test jacocoTestReport` work it
        # never asked for and doesn't need, on every single checkpoint scan
        # -- pure added failure surface (an unrelated test flake, a daemon
        # hiccup, a slower scan) for an agent that was never going to read
        # the coverage number. Only the coverage agent sets
        # with_coverage=True (see sonar/checkpoint.py's
        # TriggerAndReconcileScanStep and agent_coverage's
        # CoverageFinalVerifyStep). When org.sonarqube ever ships its own
        # jacocoTestReport auto-run (it doesn't today -- confirmed via
        # `gradle sonar --dry-run`, which never schedules it), this whole
        # branch becomes unnecessary for every agent, not just this one.
        jacoco = with_coverage

        # --no-daemon: the agent fires many Gradle invocations in quick
        # succession against ephemeral clones -- the daemon adds cross-run
        # state (and 'daemon context mismatch' failures) for no benefit here.
        args = [gradle, "--no-daemon"]
        init_script = None
        if jacoco:
            init_script = _write_gradle_jacoco_init_script(working_dir)
            args += ["--init-script", init_script, "test", "jacocoTestReport"]
        args.append("sonar")
        # --info is required, not cosmetic: the plugin logs its own "ANALYSIS
        # SUCCESSFUL ... More about the report processing at .../api/ce/task?id="
        # line at INFO level through its embedded SLF4J logger, which Gradle's
        # default LIFECYCLE threshold suppresses -- the line is simply absent
        # without it, and _parse_ce_task_id needs it.
        #
        # -Dsonar.token= (not the SONAR_TOKEN env var): a project's own
        # hardcoded `property "sonar.token", "..."` in the sonar{} DSL block
        # silently wins over the env var; -D properties DO override the DSL.
        args += [
            "--info",
            f"-Dsonar.host.url={sonar_base_url}", f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.token={sonar_token}",
        ]
        if jacoco:
            # --continue so one flaky pre-existing test can't block the scan;
            # the explicit path removes any reliance on the plugin's own
            # auto-detection of the report location.
            args += [
                "--continue",
                f"-Dsonar.coverage.jacoco.xmlReportPaths={_GRADLE_JACOCO_XML_PATH}",
            ]
        if branch:
            args.append(f"-Dsonar.branch.name={branch}")

        # init_script is left in place on purpose (stable path, constant
        # content) -- re-created identically next scan, never committed.
        result = _run(args, cwd=working_dir, timeout=1200)
        # The scan itself succeeded if the CE task URL is in the output -- true
        # even when --continue produced a non-zero exit from an unrelated
        # failing test elsewhere in the suite.
        try:
            return _parse_ce_task_id(result.stdout + result.stderr)
        except RuntimeError:
            tasks = " ".join(t for t in ("test", "jacocoTestReport", "sonar") if t in args)
            raise RuntimeError(f"Sonar scan failed (gradle {tasks}):\n{_combined_output(result)[-4000:]}")


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

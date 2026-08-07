"""
LanguageAdapter interface (workflow doc, Section 2).

Deliberately NOT an ADK agent or tool itself — it's a plain Python interface
that tool functions delegate to based on state[LANGUAGE]. Keeping it outside
the ADK object graph is what makes "add a new language later" (Section 6.5)
require zero orchestration changes: you implement one new subclass here and
register it in ADAPTER_REGISTRY, nothing in agents.py or tools/ changes.
"""

import os
import platform
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _install_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return "e.g. `brew install openjdk maven gradle` on macOS"
    if system == "Windows":
        return "e.g. `winget install EclipseAdoptium.Temurin.21.JDK` and Maven/Gradle via winget or choco on Windows"
    return "e.g. `apt install openjdk-21-jdk maven gradle` (or your distro's package manager) on Linux"


@dataclass
class BuildResult:
    passed: bool
    errors: str = ""


class ToolNotAvailableError(Exception):
    """Raised by preflight_check() when a required binary (java, mvn,
    gradle, ...) isn't on PATH. Deliberately NOT caught anywhere in
    agents.py — it's meant to propagate out of SetupStep and stop the run
    immediately, before any Sonar fetch or LLM call happens, rather than
    fail confusingly mid-pipeline on the first quick_compile_check()."""
    pass


class BuildToolNotDetectedError(Exception):
    """Raised when neither pom.xml nor build.gradle[.kts] is found (or
    when detection is ambiguous) so the run stops before guessing."""
    pass


class SonarConfigNotFoundError(Exception):
    """Raised by get_project_key() when the checked-out repo's own build
    file has no usable Sonar project key configured (no sonar.projectKey
    property, and — for Maven — no groupId/artifactId to fall back to).
    Deliberately NOT caught in agents.py — same "fail fast before any
    Sonar fetch or LLM call" contract as ToolNotAvailableError /
    BuildToolNotDetectedError, propagated out of SetupStep and surfaced as
    a clean stop by both run_local.py and IntakeStep's chat path."""
    pass


class SonarPreflightError(Exception):
    """Raised by sonar_tools.validate_connection()/check_project_analyzed()
    when the Sonar server itself isn't in a usable state for this run —
    unreachable, token rejected, or the resolved project key has never
    actually been analyzed there (a project key that resolves cleanly from
    pom.xml/build.gradle can still be one nobody has scanned yet, or
    scanned under a different key — observed live: the agent would
    otherwise create a branch and silently find 0 issues, with nothing
    telling the user why). Same "fail fast before any branch is created or
    issue fetched" contract as the other preflight exceptions above."""
    pass


def _tool_on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _run(args: list[str], cwd: str, timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, returncode=124, stdout=e.stdout or "", stderr=f"timed out after {timeout}s")


def _combined_output(result: subprocess.CompletedProcess) -> str:
    """Combines stdout+stderr instead of picking one. The previous
    `result.stderr or result.stdout` pattern picked stderr whenever it was
    non-empty AT ALL -- but JVM startup/deprecation warnings (native
    access, sun.misc.Unsafe, final-field-mutation -- all extremely common
    on modern JDKs) also go to stderr, silently discarding stdout even
    when it held the actual Maven/Gradle [ERROR] failure summary.
    Confirmed live: a real `mvn sonar:sonar` failure surfaced as nothing
    but JVM warning noise, with zero information about the actual cause.
    stderr first, stdout last: every caller here truncates to the LAST N
    chars, and a build tool's real error summary is almost always at the
    END of stdout, so this ordering keeps that inside the truncation
    window instead of the warnings pushing it out."""
    return f"{result.stderr or ''}\n{result.stdout or ''}".strip()


_CE_TASK_ID_RE = re.compile(r"api/ce/task\?id=([\w-]+)")


def _parse_ce_task_id(output: str) -> str:
    """Every Sonar scanner integration (Gradle/Maven plugins, standalone
    CLI) prints an identical 'More about the report processing at
    .../api/ce/task?id=<id>' line on success — this format is stable
    across scanner versions and is the documented way to get the
    background task id for polling."""
    m = _CE_TASK_ID_RE.search(output)
    if not m:
        raise RuntimeError(
            "Sonar scan finished but no Compute Engine task id found in its "
            f"output — can't poll for processing status. Last output:\n{output[-2000:]}"
        )
    return m.group(1)


class LanguageAdapter(ABC):
    @abstractmethod
    def preflight_check(self, working_dir: str) -> None:
        """Raises ToolNotAvailableError with a clear, actionable message if
        anything this adapter needs (java, mvn/gradle, wrapper scripts) is
        missing. Called once in SetupStep before any other work starts."""
        ...

    @abstractmethod
    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult: ...

    @abstractmethod
    def verify_build(self, working_dir: str) -> BuildResult: ...

    @abstractmethod
    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        """Runs only the named test classes (fully-qualified, e.g.
        'portal.expenses.controller.AuthControllerTest') instead of the
        whole suite. Used to verify a just-re-enabled S2187 test in
        isolation, on its own file, before it's bundled into a checkpoint
        batch with unrelated files — quick_compile_check() never runs
        tests at all, and waiting for the shared checkpoint's full
        verify_build() means one broken re-enabled test drags every other
        file in that batch into a collateral bisect-revert with it."""
        ...

    @abstractmethod
    def get_source_root(self, working_dir: str) -> str: ...

    @abstractmethod
    def get_fix_prompt_addendum(self) -> str:
        """Language-specific guidance block (Sections 6.2-6.4). Appended to
        the shared skeleton in prompts.py — never duplicated per-language."""
        ...

    @abstractmethod
    def get_project_key(self, working_dir: str) -> str:
        """Reads the Sonar project key straight from this build tool's own
        config (the sonar{} DSL block / gradle.properties for Gradle, the
        <sonar.projectKey> property or groupId:artifactId default for
        Maven) rather than trusting a value passed in from outside. Mirrors
        the sonar.token precedence fix in run_sonar_scan()'s docstring —
        just inverted: there the build file's own value was the stale one
        to override, here it's the source of truth, since the project key
        actually configured in the build is what the Sonar plugin invoked
        by run_sonar_scan() will use regardless of what's in .env. Raises
        SonarConfigNotFoundError with an actionable message if it can't be
        found."""
        ...

    @abstractmethod
    def run_sonar_scan(
        self, working_dir: str, sonar_base_url: str, sonar_token: str,
        project_key: str, branch: str | None = None,
    ) -> str:
        """Runs this build tool's Sonar plugin synchronously (Section 7)
        and returns the Compute Engine task id to poll. branch=None means
        a local/CE-edition scan — Community Edition rejects
        sonar.branch.name outright, so it's only passed for Developer+.

        sonar_token MUST be passed as an explicit -Dsonar.token= property,
        not the SONAR_TOKEN env var — verified live that a project's own
        hardcoded `sonar { properties { property "sonar.token", "..." } }`
        (as this project's build.gradle has) silently wins over the env
        var, so relying on it would authenticate with a stale/leaked
        in-repo token instead of the one actually configured in .env. A
        -D property does correctly override the build script's own DSL
        block (confirmed the same way: an invalid -Dsonar.token fails the
        scan outright, an invalid SONAR_TOKEN env var does not)."""
        ...


class JavaMavenAdapter(LanguageAdapter):
    def _mvn_cmd(self, working_dir: str) -> str:
        # Prefer the wrapper if the project ships one — pins the exact
        # Maven version the project expects, avoids "works on my machine".
        # Windows' wrapper is mvnw.cmd, not the Unix mvnw shell script — and
        # an absolute path (rather than "./mvnw") sidesteps any ambiguity
        # about whether a bare relative command name resolves against cwd,
        # which differs across OS/subprocess implementations.
        wrapper = os.path.join(working_dir, "mvnw.cmd" if _is_windows() else "mvnw")
        return wrapper if os.path.isfile(wrapper) else "mvn"

    def preflight_check(self, working_dir: str) -> None:
        missing = []
        if not _tool_on_path("java"):
            missing.append("java (JDK)")
        mvn_cmd = self._mvn_cmd(working_dir)
        if mvn_cmd == "mvn" and not _tool_on_path("mvn"):
            missing.append("mvn (Maven) — no mvnw wrapper found either")
        if missing:
            raise ToolNotAvailableError(
                f"Cannot build this project: missing required tool(s): {', '.join(missing)}. "
                f"Install them ({_install_hint()}) or ensure they're "
                f"on PATH, then re-run. Stopping before any Sonar fetch or fix generation."
            )

    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult:
        mvn = self._mvn_cmd(working_dir)
        # scope is a module/subdirectory path when the repo is multi-module;
        # -pl fails harmlessly with a clear error on single-module repos
        # where scope doesn't resolve to a module, which is caught below.
        result = _run([mvn, "-q", "-pl", scope, "-am", "compile"], cwd=working_dir, timeout=180)
        if result.returncode != 0:
            # fall back to a project-wide compile in case `scope` isn't a
            # real Maven module (e.g. single-module repo, scope == file path)
            result = _run([mvn, "-q", "compile"], cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def verify_build(self, working_dir: str) -> BuildResult:
        mvn = self._mvn_cmd(working_dir)
        # -DskipITs: skips maven-failsafe-plugin's integration-test/verify
        # goals (anything matching *IT.java, *ITCase.java, IT*.java — e2e/UI
        # tests like Playwright/Selenium specs live here) while still
        # running the regular unit tests via surefire. Scope is Sonar
        # findings in Java source, verified by real unit tests — a
        # browser-automation timeout has nothing to do with a Sonar fix and
        # was observed live (WebGoat's LoginUITest) forcing a checkpoint
        # revert of otherwise-correct changes.
        result = _run([mvn, "-q", "verify", "-DskipITs"], cwd=working_dir, timeout=1800)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        mvn = self._mvn_cmd(working_dir)
        # Surefire matches -Dtest by simple class name, not FQCN — fine
        # here since collisions across packages are rare and this is a
        # best-effort isolation check, not the source of truth (verify_build
        # still runs the real full suite at the checkpoint).
        simple_names = [c.rsplit(".", 1)[-1] for c in test_classes]
        result = _run(
            [mvn, "-q", "test", f"-Dtest={','.join(simple_names)}"],
            cwd=working_dir, timeout=300,
        )
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def get_source_root(self, working_dir: str) -> str:
        return os.path.join(working_dir, "src", "main", "java")

    def get_fix_prompt_addendum(self) -> str:
        from ..prompts import JAVA_SPRING_ADDENDUM
        return JAVA_SPRING_ADDENDUM

    def get_project_key(self, working_dir: str) -> str:
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
        # when sonar.projectKey isn't set explicitly — groupId is frequently
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
        args = [
            mvn, "sonar:sonar",
            f"-Dsonar.host.url={sonar_base_url}", f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.token={sonar_token}",
        ]
        if branch:
            args.append(f"-Dsonar.branch.name={branch}")
        result = _run(args, cwd=working_dir, timeout=900)
        if result.returncode != 0:
            raise RuntimeError(f"Sonar scan failed (mvn sonar:sonar):\n{_combined_output(result)[-3000:]}")
        return _parse_ce_task_id(result.stdout + result.stderr)


class JavaGradleAdapter(LanguageAdapter):
    @staticmethod
    def _wrapper_name() -> str:
        # Windows' wrapper is gradlew.bat, not the Unix gradlew shell
        # script — .bat/.cmd files are directly executable via subprocess
        # on Windows without shell=True, so no other invocation change
        # is needed once the right filename is picked.
        return "gradlew.bat" if _is_windows() else "gradlew"

    def _gradle_wrapper_usable(self, working_dir: str) -> bool:
        # A `gradlew`/`gradlew.bat` script alone isn't enough — it's a thin
        # launcher that does `java -jar gradle/wrapper/gradle-wrapper.jar`,
        # and that jar is a binary many repos gitignore. A fresh clone can
        # have the script but not the jar, which fails opaquely ("Unable to
        # access jarfile ...") deep inside a build call rather than here,
        # wasting an entire run's worth of fetch/fix work before surfacing.
        # Check both.
        return os.path.isfile(os.path.join(working_dir, self._wrapper_name())) and os.path.isfile(
            os.path.join(working_dir, "gradle", "wrapper", "gradle-wrapper.jar")
        )

    def _gradle_cmd(self, working_dir: str) -> str:
        # Absolute path (rather than "./gradlew") sidesteps any ambiguity
        # about whether a bare relative command name resolves against cwd,
        # which differs across OS/subprocess implementations.
        if self._gradle_wrapper_usable(working_dir):
            return os.path.join(working_dir, self._wrapper_name())
        return "gradle"

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
            with open(path) as f:
                m = self._PROJECT_KEY_PROPERTY_RE.search(f.read())
            if m:
                return m.group(1)

        props_path = os.path.join(working_dir, "gradle.properties")
        if os.path.isfile(props_path):
            with open(props_path) as f:
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

    def preflight_check(self, working_dir: str) -> None:
        missing = []
        if not _tool_on_path("java"):
            missing.append("java (JDK)")
        gradle_cmd = self._gradle_cmd(working_dir)
        if gradle_cmd == "gradle" and not _tool_on_path("gradle"):
            has_gradlew = os.path.isfile(os.path.join(working_dir, self._wrapper_name()))
            reason = (
                "gradlew is present but gradle/wrapper/gradle-wrapper.jar is missing "
                "(likely gitignored in this repo) — no usable wrapper"
                if has_gradlew else "no gradlew wrapper found either"
            )
            missing.append(f"gradle — {reason}")
        if missing:
            raise ToolNotAvailableError(
                f"Cannot build this project: missing required tool(s): {', '.join(missing)}. "
                f"Install them ({_install_hint()}) or ensure they're "
                f"on PATH, then re-run. Stopping before any Sonar fetch or fix generation."
            )

    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult:
        gradle = self._gradle_cmd(working_dir)
        # scope as a Gradle module path, e.g. "my-module" -> ":my-module:compileJava"
        task = f":{scope}:compileJava" if scope and not scope.startswith(":") else "compileJava"
        result = _run([gradle, "-q", task, "compileTestJava"], cwd=working_dir, timeout=180)
        if result.returncode != 0:
            result = _run([gradle, "-q", "compileJava", "compileTestJava"], cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def verify_build(self, working_dir: str) -> BuildResult:
        gradle = self._gradle_cmd(working_dir)
        # `test` only, not `build`/`check` — `check` aggregates every
        # verification task a project wires up, which on some repos
        # includes separate e2e/UI source sets (Playwright/Selenium) with
        # their own task. Scope is Sonar findings in Java source, verified
        # by real unit tests; `test` runs those (compileJava,
        # compileTestJava, test) without pulling in browser-automation
        # tasks that are unrelated to a Sonar fix and prone to
        # environment-driven flakiness (see the Maven adapter's -DskipITs
        # for the same reasoning).
        result = _run([gradle, "-q", "test"], cwd=working_dir, timeout=1800)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        gradle = self._gradle_cmd(working_dir)
        args = [gradle, "-q", "test"]
        for c in test_classes:
            args += ["--tests", c]
        result = _run(args, cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def get_source_root(self, working_dir: str) -> str:
        return os.path.join(working_dir, "src", "main", "java")

    def get_fix_prompt_addendum(self) -> str:
        from ..prompts import JAVA_SPRING_ADDENDUM
        return JAVA_SPRING_ADDENDUM

    def run_sonar_scan(self, working_dir, sonar_base_url, sonar_token, project_key, branch=None):
        gradle = self._gradle_cmd(working_dir)
        # --info is required, not cosmetic: this plugin logs its own
        # "ANALYSIS SUCCESSFUL ... More about the report processing at
        # .../api/ce/task?id=..." line at INFO level through its embedded
        # SLF4J logger, which Gradle's default LIFECYCLE log threshold
        # suppresses — verified live, the line is simply absent without it.
        #
        # -Dsonar.token= (not the SONAR_TOKEN env var): verified live that
        # this project's build.gradle has its own hardcoded
        # `property "sonar.token", "..."` in the sonar{} DSL block, and
        # that value silently wins over SONAR_TOKEN — an invalid env var
        # still authenticated successfully. -D properties DO correctly
        # override the DSL block (an invalid -Dsonar.token fails outright),
        # so that's the only reliable way to guarantee .env's token is
        # what's actually used, not whatever a repo has baked in.
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
    "java-maven": JavaMavenAdapter,
    "java-gradle": JavaGradleAdapter,
    # "python": PythonAdapter,        # add later, Section 6.5
    # "typescript": TypeScriptAdapter,
}


def detect_build_tool(working_dir: str) -> str:
    """Inspects the checked-out repo root for build files and returns
    'java-maven' or 'java-gradle'. Raises BuildToolNotDetectedError rather
    than guessing if neither or both are present — an agent silently
    picking the wrong build tool on a mixed/migrating repo is worse than
    stopping and asking."""
    has_maven = os.path.isfile(os.path.join(working_dir, "pom.xml"))
    has_gradle = any(
        os.path.isfile(os.path.join(working_dir, f))
        for f in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    )
    if has_maven and has_gradle:
        raise BuildToolNotDetectedError(
            f"Both pom.xml and a Gradle build file exist at {working_dir} — "
            f"ambiguous. Set LANGUAGE explicitly to 'java-maven' or 'java-gradle' in .env."
        )
    if has_maven:
        return "java-maven"
    if has_gradle:
        return "java-gradle"
    raise BuildToolNotDetectedError(
        f"No pom.xml or build.gradle[.kts] found at {working_dir} — "
        f"can't determine the Java build tool. Set LANGUAGE explicitly in .env if this "
        f"is a non-standard layout."
    )


def get_adapter(language: str, working_dir: str | None = None) -> LanguageAdapter:
    """language == 'java' triggers auto-detection against working_dir.
    Anything else (e.g. explicit 'java-maven'/'java-gradle') is used as-is —
    an explicit LANGUAGE in .env always wins over detection, which matters
    for the ambiguous mixed-repo case above."""
    resolved = language
    if language == "java":
        if working_dir is None:
            raise ValueError("working_dir is required to auto-detect a 'java' project's build tool")
        resolved = detect_build_tool(working_dir)

    if resolved not in ADAPTER_REGISTRY:
        raise ValueError(f"No LanguageAdapter registered for '{resolved}'")
    return ADAPTER_REGISTRY[resolved]()

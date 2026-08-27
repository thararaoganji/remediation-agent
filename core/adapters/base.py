"""
LanguageAdapter interface — the tool-agnostic half.

Deliberately NOT an ADK agent or tool itself — it's a plain Python interface
that step classes delegate to based on state[LANGUAGE]. Keeping it outside
the ADK object graph is what makes "add a new language later" require zero
orchestration changes: implement one new subclass here and register it in
ADAPTER_REGISTRY, nothing in any agent package changes.

get_project_key() and run_sonar_scan() deliberately do NOT live here, even
though the original single-package version of this file had them on this
same interface. Both answer a question specific to *one* finding source
("what key does Sonar know this project by", "how do I invoke Sonar's
plugin") — a Veracode or Black Duck integration has its own, different
answer to both, so baking Sonar's shape into this shared interface would
misrepresent it as universal. sonar/adapters.py adds them via subclassing
(SonarJavaMavenAdapter(JavaMavenAdapter), SonarJavaGradleAdapter(JavaGradleAdapter))
instead.
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
    gradle, ...) isn't on PATH. Deliberately NOT caught anywhere in the
    agent packages — it's meant to propagate out of a setup step and stop
    the run immediately, before any finding fetch or LLM call happens,
    rather than fail confusingly mid-pipeline on the first
    quick_compile_check()."""
    pass


class BuildToolNotDetectedError(Exception):
    """Raised when neither pom.xml nor build.gradle[.kts] is found (or
    when detection is ambiguous) so the run stops before guessing."""
    pass


def _tool_on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _resolve_tool_on_path(name: str) -> str | None:
    if not _tool_on_path(name):
        return None
    return shutil.which(name)


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


class LanguageAdapter(ABC):
    @abstractmethod
    def preflight_check(self, working_dir: str) -> None:
        """Raises ToolNotAvailableError with a clear, actionable message if
        anything this adapter needs (java, mvn/gradle, wrapper scripts) is
        missing. Called once in setup, before any other work starts."""
        ...

    @abstractmethod
    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult: ...

    @abstractmethod
    def verify_build(self, working_dir: str) -> BuildResult: ...

    @abstractmethod
    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        """Runs only the named test classes (fully-qualified, e.g.
        'portal.expenses.controller.AuthControllerTest') instead of the
        whole suite. Used to verify a just-changed/re-enabled test in
        isolation, on its own file, before it's bundled into a checkpoint
        batch with unrelated files — quick_compile_check() never runs
        tests at all, and waiting for the shared checkpoint's full
        verify_build() means one broken test drags every other file in
        that batch into a collateral bisect-revert with it."""
        ...

    @abstractmethod
    def get_source_root(self, working_dir: str) -> str: ...

    @abstractmethod
    def get_fix_prompt_addendum(self) -> str:
        """Language-specific guidance block, appended to a shared prompt
        skeleton -- never duplicated per-language."""
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
        if os.path.isfile(wrapper):
            return wrapper
        if _is_windows():
            resolved = _resolve_tool_on_path("mvn")
            if resolved:
                return resolved
        return "mvn"

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
                f"on PATH, then re-run. Stopping before any finding fetch or fix generation."
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
        # running the regular unit tests via surefire.
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
        return ""


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
        if _is_windows():
            resolved = _resolve_tool_on_path("gradle")
            if resolved:
                return resolved
        return "gradle"

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
                f"on PATH, then re-run. Stopping before any finding fetch or fix generation."
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
        # their own task. `test` runs compileJava, compileTestJava, test
        # without pulling in browser-automation tasks that are prone to
        # environment-driven flakiness.
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
        return ""


ADAPTER_REGISTRY = {
    "java-maven": JavaMavenAdapter,
    "java-gradle": JavaGradleAdapter,
    # "python": PythonAdapter,        # add later
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

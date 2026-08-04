"""
LanguageAdapter interface (workflow doc, Section 2).

Deliberately NOT an ADK agent or tool itself — it's a plain Python interface
that tool functions delegate to based on state[LANGUAGE]. Keeping it outside
the ADK object graph is what makes "add a new language later" (Section 6.5)
require zero orchestration changes: you implement one new subclass here and
register it in ADAPTER_REGISTRY, nothing in agents.py or tools/ changes.
"""

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


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


def _tool_on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _run(args: list[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, returncode=124, stdout=e.stdout or "", stderr=f"timed out after {timeout}s")


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
    def get_source_root(self, working_dir: str) -> str: ...

    @abstractmethod
    def get_fix_prompt_addendum(self) -> str:
        """Language-specific guidance block (Sections 6.2-6.4). Appended to
        the shared skeleton in prompts.py — never duplicated per-language."""
        ...

    @abstractmethod
    def parse_and_validate_patch(self, diff: str, working_dir: str) -> BuildResult:
        """Syntax-only check that a diff applies and produces parseable
        source. NOT a semantic guarantee — see checkpoint verify_build()
        for that."""
        ...


class JavaMavenAdapter(LanguageAdapter):
    def _mvn_cmd(self, working_dir: str) -> str:
        # Prefer the wrapper if the project ships one — pins the exact
        # Maven version the project expects, avoids "works on my machine".
        return "./mvnw" if os.path.isfile(os.path.join(working_dir, "mvnw")) else "mvn"

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
                f"Install them (e.g. `brew install openjdk maven` on macOS) or ensure they're "
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
        return BuildResult(passed=result.returncode == 0, errors=result.stderr or result.stdout)

    def verify_build(self, working_dir: str) -> BuildResult:
        mvn = self._mvn_cmd(working_dir)
        result = _run([mvn, "-q", "verify"], cwd=working_dir, timeout=1800)
        return BuildResult(passed=result.returncode == 0, errors=result.stderr or result.stdout)

    def get_source_root(self, working_dir: str) -> str:
        return f"{working_dir}/src/main/java"

    def get_fix_prompt_addendum(self) -> str:
        from ..prompts import JAVA_SPRING_ADDENDUM
        return JAVA_SPRING_ADDENDUM

    def parse_and_validate_patch(self, diff: str, working_dir: str) -> BuildResult:
        raise NotImplementedError("apply diff to temp copy, run javac -Xlint syntax-only")


class JavaGradleAdapter(LanguageAdapter):
    def _gradle_cmd(self, working_dir: str) -> str:
        return "./gradlew" if os.path.isfile(os.path.join(working_dir, "gradlew")) else "gradle"

    def preflight_check(self, working_dir: str) -> None:
        missing = []
        if not _tool_on_path("java"):
            missing.append("java (JDK)")
        gradle_cmd = self._gradle_cmd(working_dir)
        if gradle_cmd == "gradle" and not _tool_on_path("gradle"):
            missing.append("gradle — no gradlew wrapper found either")
        if missing:
            raise ToolNotAvailableError(
                f"Cannot build this project: missing required tool(s): {', '.join(missing)}. "
                f"Install them (e.g. `brew install openjdk gradle` on macOS) or ensure they're "
                f"on PATH, then re-run. Stopping before any Sonar fetch or fix generation."
            )

    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult:
        gradle = self._gradle_cmd(working_dir)
        # scope as a Gradle module path, e.g. "my-module" -> ":my-module:compileJava"
        task = f":{scope}:compileJava" if scope and not scope.startswith(":") else "compileJava"
        result = _run([gradle, "-q", task, "compileTestJava"], cwd=working_dir, timeout=180)
        if result.returncode != 0:
            result = _run([gradle, "-q", "compileJava", "compileTestJava"], cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=result.stderr or result.stdout)

    def verify_build(self, working_dir: str) -> BuildResult:
        gradle = self._gradle_cmd(working_dir)
        result = _run([gradle, "-q", "build"], cwd=working_dir, timeout=1800)
        return BuildResult(passed=result.returncode == 0, errors=result.stderr or result.stdout)

    def get_source_root(self, working_dir: str) -> str:
        return f"{working_dir}/src/main/java"

    def get_fix_prompt_addendum(self) -> str:
        from ..prompts import JAVA_SPRING_ADDENDUM
        return JAVA_SPRING_ADDENDUM

    def parse_and_validate_patch(self, diff: str, working_dir: str) -> BuildResult:
        raise NotImplementedError("apply diff to temp copy, run javac -Xlint syntax-only")


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

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

import json
import os
import platform
import shutil
import subprocess
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

    @abstractmethod
    def test_identifier(self, test_file_path: str) -> str:
        """Converts a test file's repo-relative path into whatever
        run_specific_tests()'s test_classes argument actually expects for
        this language -- a fully-qualified class name for Maven/Gradle
        (Surefire/the `test` task match by class, not path), the path
        itself for anything whose test runner filters by file (Karma's
        --include, Vitest's positional file filter)."""
        ...

    @abstractmethod
    def production_to_test_path(self, file_path: str) -> str:
        """Converts a production source file's repo-relative path to
        where its test file lives/belongs, per this language's own
        convention -- Maven/Gradle's separate src/test/java tree with a
        Test suffix, Angular's co-located Component.ts -> Component.spec.ts
        in the same directory, etc."""
        ...

    def prepare_workspace(self, working_dir: str) -> None:
        """Optional hook, run once right after preflight_check(), before any
        other work -- for anything that must happen before a single
        compile/test/build call can succeed at all, but isn't itself a pure
        capability check (preflight_check's contract). Default no-op: Java's
        Maven/Gradle resolve dependencies on demand as part of the first
        real build call, so neither adapter needs this. npm-based adapters
        override it to run `npm ci` -- node_modules must already exist
        before `ng`/`tsc`/`vitest` can run at all, which has no Java
        equivalent."""
        return None


_JAVA_FIX_PROMPT_ADDENDUM = """\
LANGUAGE-SPECIFIC GUIDANCE (Java / Spring Boot):
- Test code (new or modified): JUnit 5 + Mockito, matching this codebase's
  existing conventions where visible — this is a Spring project's standard
  stack, use it even if the file being tested currently has no test file to
  copy the style from.
- Preserve Spring annotations and bean wiring exactly (@Service, @Repository,
  @Autowired/constructor injection, @Transactional boundaries, @RequestMapping
  and related). If a SECURITY or RELIABILITY fix requires touching a
  @Transactional method, do not change its propagation/isolation semantics
  unless the issue is specifically about that.
- Prefer constructor injection over field injection when fixing issues that
  touch dependency injection, but only if the file isn't already consistently
  using field injection elsewhere; don't mix styles within one file.
- For security issues (hardcoded credentials S2068, SQL injection S3649,
  weak crypto S4426): fix via Spring's standard mechanisms rather than ad hoc
  workarounds.
- After fixing, the file must remain valid for the project's declared Java
  language level — do not use syntax newer than that.

STRICT JAVA REMEDIATION RULES (per-rule guidance from observed checkpoint
failures — these are more specific than the general guidance above and win
on conflict for the rule keys they name):
1. No broad suppressions. The only rules an @SuppressWarnings is allowed for:
   stateless CSRF (java:S4502), JPA param counts (java:S107), or marker
   interfaces (java:S2094).
2. S6242/S1874 (AWS): use DefaultCredentialsProvider.builder().build(). Do
   NOT use .create().
3. S6809/S6813 (self-invocation / @Transactional proxy bypass): fix via
   SETTER injection (@Autowired @Lazy on a setter), NOT field or constructor
   self-injection — a self-referencing constructor/field injection risks a
   BeanCurrentlyInCreationException that fails the whole application
   context, breaking the full build, not just this file.

   MANDATORY at every call site that invokes the self-injected field: guard
   it against being null and fall back to `this`.
     Violation: return self.createExpense(...);
     Fix:       return (self != null ? self : this).createExpense(...);
   The self-referencing field is only ever wired by a real Spring context at
   runtime — Mockito's @InjectMocks has no way to satisfy it, so any existing
   Mockito-only unit test (@ExtendWith(MockitoExtension.class), no Spring
   context) of this class WILL NullPointerException the moment it calls a
   method that goes through the self-proxy if this guard is missing, even
   though nothing in the test itself changed. Confirmed live, twice, on the
   same class in the same project: one fix attempt included this guard and
   the checkpoint passed; a separate attempt at the identical issue omitted
   it and broke the checkpoint's unit tests. This is not a stylistic
   preference — omitting it is the direct cause of a build failure.

   If this class already has a Mockito-only unit test AND its content is
   actually visible to you in this prompt (it usually will not be — only
   THIS file's content is provided), you may additionally add
   `instance.setSelf(instance);` to that test's @BeforeEach/setup as a
   belt-and-suspenders fix — an explicit exception to "fix only the listed
   file," the same carve-out rule 9 makes for S1948. The null-safe fallback
   above is mandatory regardless of whether you do this, since the test
   file usually isn't available to edit correctly from this prompt alone.
4. S6208/S6880 (switch expressions): replace if/else chains with a Java 21+
   switch expression (only if the project's language level supports it — see
   the general guidance above). Always handle null safely:
     return switch (obj) {
         case null -> throw new IllegalArgumentException("Target is null");
         case String s -> "string type";
         default -> "other type";
     };
5. S6877/S6916 (pattern match guards): replace a nested if inside a pattern
   match/case block with a `when` guard instead. Do NOT write nested ifs.
     Violation: case String s -> { if (s.isEmpty()) { ... } }
     Fix:       case String s when s.isEmpty() -> ...
6. S2629 (logging): never evaluate methods or concatenate strings inside
   logger parameters. Pass raw objects as {} placeholders, or guard with an
   explicit if (log.isXEnabled()) block.
     Violation: log.debug("error: " + err.getMessage());
     Fix:       log.debug("error: {}", err);
7. S2187 (disabled tests): restore the test (remove the comment-out), and
   add whatever @MockBean(s) are needed to fix any resulting
   UnsatisfiedDependencyException rather than leaving the test broken.
   A disabled test has never actually been run — do not assume it's only
   missing whatever ONE dependency happens to be visible from a quick read;
   check the ENTIRE constructor signature of the class under test (and, for
   a @WebMvcTest, of every class the test's @Import(...) list pulls in too)
   against what the test provides via @Autowired/@MockBean. Every
   constructor parameter of every one of those classes needs a matching
   bean or an explicit @MockBean — a test that still fails to load its
   ApplicationContext after your fix is worse than leaving it disabled,
   since it now breaks the full build instead of just sitting inert.
8. S5778 (assertThrows): extract any setup/construction (e.g. `new
   BigDecimal(...)`, `LocalDate.now()`) OUTSIDE the assertThrows lambda —
   the lambda must contain exactly one method call.
9. S1948 (serialization): you MAY modify related classes outside the
   flagged file to implement Serializable and add a serialVersionUID — this
   is the one explicit exception to "fix only the listed file."
10. S2068/S6437 (hardcoded credentials): when replacing a hardcoded literal
    with an externalized value (System.getenv/@Value/config), do NOT add a
    hardcoded literal as the fallback for when it's missing — a fallback
    string assigned to a password/secret/token-named variable is ITSELF
    exactly the pattern these rules flag, so it just re-triggers the same
    finding under a different guise (observed live: `String x =
    System.getenv("X"); if (x == null) { x = "someLiteralDefault"; }`
    still gets flagged, because the fallback assignment IS a hardcoded
    credential-shaped literal). Fail fast instead — throw
    IllegalStateException (or the class's existing exception convention) if
    the externalized value is missing, rather than silently substituting a
    fake one.
      Violation: String pw = System.getenv("PW"); if (pw == null) { pw = "default"; }
      Fix:       String pw = System.getenv("PW");
                 if (pw == null) { throw new IllegalStateException("PW must be set"); }

DEFENSIVE REFACTORING (avoid introducing new Medium-severity findings):
before finalizing your diff, check it doesn't introduce any of these:
11. S1128 (unused imports): if your fix removed the last usage of an
    imported class, delete that import. Never introduce a wildcard (`.*`)
    import.
12. S1481/S1068 (unused locals/fields): don't leave an orphaned local
    variable or private field that's no longer read.
13. S1192 (duplicated string literals): don't let the same string literal
    appear 3+ times — extract it to a private static final String constant.
14. S3776 (cognitive complexity): if your fix adds multiple try/catch or
    if/else blocks, extract the inner logic into a private helper method
    instead of nesting it inline.
15. Scope: only modify the reported file — the named exceptions are rule 9
    (S1948, may touch related classes for Serializable) and rule 3 (S6809/
    S6813, may touch this class's own existing Mockito unit test file to
    wire the self-reference). Otherwise do not touch unrelated code even if
    you notice other problems in it.
"""


def _java_fqcn(file_path: str) -> str:
    """Converts a Java source path (relative to the repo root) to its
    fully-qualified class name — e.g.
    'src/test/java/portal/expenses/controller/AuthControllerTest.java' ->
    'portal.expenses.controller.AuthControllerTest'. Assumes the standard
    Maven/Gradle layout (a 'java/' segment marking the source root).
    Works for both src/main/java and src/test/java, since it only anchors
    on the literal 'java' folder name. Shared by both Java adapters (their
    test_identifier()) -- identical for Maven and Gradle."""
    parts = file_path.replace("\\", "/").split("/")
    if "java" in parts:
        parts = parts[parts.index("java") + 1:]
    joined = "/".join(parts)
    if joined.endswith(".java"):
        joined = joined[: -len(".java")]
    return joined.replace("/", ".")


def _java_test_file_path(file_path: str) -> str:
    """src/main/java/.../Foo.java -> src/test/java/.../FooTest.java. Only
    inserts the Test suffix if the file doesn't already end in one (so a
    file already named ...Test.java — unusual for a production class, but
    not impossible — doesn't get double-suffixed). Shared by both Java
    adapters (their production_to_test_path()) -- identical for Maven and
    Gradle."""
    path = file_path.replace("\\", "/")
    if "/main/" in path:
        path = path.replace("/main/", "/test/", 1)
    if path.endswith(".java") and not path.endswith("Test.java"):
        path = path[: -len(".java")] + "Test.java"
    return path


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
        return _JAVA_FIX_PROMPT_ADDENDUM

    def test_identifier(self, test_file_path: str) -> str:
        return _java_fqcn(test_file_path)

    def production_to_test_path(self, file_path: str) -> str:
        return _java_test_file_path(file_path)


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
        return _JAVA_FIX_PROMPT_ADDENDUM

    def test_identifier(self, test_file_path: str) -> str:
        return _java_fqcn(test_file_path)

    def production_to_test_path(self, file_path: str) -> str:
        return _java_test_file_path(file_path)


_TS_FIX_PROMPT_ADDENDUM = """\
LANGUAGE-SPECIFIC GUIDANCE (Angular / TypeScript):
- Standalone components are this project's convention (no NgModules) —
  don't introduce an NgModule-based pattern (declarations/imports arrays,
  a *.module.ts file) unless the file you're fixing already uses one.
- Prefer the `inject()` function for dependency injection over constructor
  injection when adding a new dependency to an existing standalone
  component/service, but only if the file isn't already consistently using
  constructor injection — don't mix styles within one file.
- RxJS subscriptions: every `.subscribe(...)` your fix adds or touches must
  be cleaned up (`takeUntilDestroyed()`, `async` pipe in the template, or an
  explicit unsubscribe) — an uncleaned subscription is a memory leak, not a
  style nit.
- Never introduce `any` — use the real type, `unknown` with a narrowing
  check, or a generic, matching whatever strictness `tsconfig.json` already
  enforces.
- This project uses Angular Material — prefer its components/directives
  over hand-rolled equivalents when a fix touches template markup.
- After fixing, the file must still satisfy `tsc --noEmit` under this
  project's tsconfig — do not use a TypeScript feature newer than its
  configured target/lib.
"""


def _read_package_json(working_dir: str) -> dict | None:
    """None if package.json is missing or not valid JSON -- callers treat
    that as "can't tell, ask the user to set LANGUAGE explicitly" rather
    than guessing."""
    path = os.path.join(working_dir, "package.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return None


def _package_deps(pkg: dict) -> dict:
    return {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}


def _angular_production_to_test_path(file_path: str) -> str:
    """Component.ts -> Component.spec.ts, same directory -- Angular's
    co-located test convention (tsconfig.spec.json's own `include` is
    `src/**/*.spec.ts`, no separate src/test tree like Maven/Gradle).
    Shared by every TypeScript adapter regardless of test runner — this is
    an Angular file-layout convention, not a runner difference."""
    path = file_path.replace("\\", "/")
    if path.endswith(".spec.ts"):
        return path
    if path.endswith(".ts"):
        return path[: -len(".ts")] + ".spec.ts"
    return path


class _AngularAdapter(LanguageAdapter):
    """Shared behavior for every Angular/TypeScript adapter, regardless of
    test runner -- production_to_test_path/prepare_workspace/
    get_source_root/get_fix_prompt_addendum/test_identifier are all Angular
    file-layout or npm-tooling conventions, not test-runner-specific ones.
    Still abstract: concrete subclasses (TypeScriptKarmaAdapter,
    TypeScriptVitestAdapter) supply preflight_check/quick_compile_check/
    verify_build/run_specific_tests, which genuinely differ per runner."""

    def _missing_node_tools(self) -> list[str]:
        missing = []
        if not _tool_on_path("node"):
            missing.append("node")
        if not _tool_on_path("npm"):
            missing.append("npm")
        return missing

    def prepare_workspace(self, working_dir: str) -> None:
        # node_modules must exist before `ng`/`tsc`/`vitest` can run at
        # all -- no Java equivalent (Maven/Gradle resolve deps on demand
        # as part of the first real build call), so this can't reuse
        # preflight_check's pure-verification contract.
        result = _run(["npm", "ci"], cwd=working_dir, timeout=300)
        if result.returncode != 0:
            raise ToolNotAvailableError(
                f"`npm ci` failed in {working_dir} — can't proceed without "
                f"installed dependencies:\n{_combined_output(result)[-2000:]}"
            )

    def quick_compile_check(self, working_dir: str, scope: str) -> BuildResult:
        # scope ignored -- unlike a Maven/Gradle module, tsc type-checks the
        # whole program; there's no meaningful partial-project compile.
        result = _run(["npx", "tsc", "--noEmit", "-p", "tsconfig.app.json"], cwd=working_dir, timeout=180)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def get_source_root(self, working_dir: str) -> str:
        return os.path.join(working_dir, "src", "app")

    def get_fix_prompt_addendum(self) -> str:
        return _TS_FIX_PROMPT_ADDENDUM

    def test_identifier(self, test_file_path: str) -> str:
        # Karma's --include and Vitest's positional file filter both take a
        # path/glob, not a class name -- unlike Maven/Gradle's Surefire/
        # `test` task, which match by fully-qualified class name.
        return test_file_path

    def production_to_test_path(self, file_path: str) -> str:
        return _angular_production_to_test_path(file_path)


class TypeScriptKarmaAdapter(_AngularAdapter):
    """Angular CLI's default: Karma + Jasmine, headless Chrome. Karma has
    no built-in machine-readable reporter, so karma-junit-reporter (an
    external devDependency) is required -- preflight_check fails clearly,
    with exact install/config instructions, if it isn't wired in, rather
    than the agent silently losing its ability to name which test failed a
    checkpoint (core.tools.patch_tools.parse_junit_failures needs that
    report to exist)."""

    _JUNIT_OUTPUT_DIR = "test-results/karma"

    def preflight_check(self, working_dir: str) -> None:
        missing = self._missing_node_tools()
        if missing:
            raise ToolNotAvailableError(
                f"Cannot build this project: missing required tool(s): {', '.join(missing)}. "
                f"Install Node.js (https://nodejs.org) and ensure it's on PATH, then re-run."
            )
        pkg = _read_package_json(working_dir)
        if pkg is None:
            raise ToolNotAvailableError(
                f"No valid package.json found at {working_dir} — not an npm project, "
                f"or its package.json isn't valid JSON."
            )
        install_hint = (
            "karma-junit-reporter is not configured, but every agent's checkpoint/"
            "bisect logic needs a JUnit-XML test report to identify which specific "
            "test failed a build (the same mechanism Gradle/Maven's own test "
            "reports already provide for Java). Add it once:\n"
            "  npm install --save-dev karma-junit-reporter\n"
            "  # karma.conf.js:\n"
            "  #   plugins: [..., require('karma-junit-reporter')],\n"
            "  #   reporters: [..., 'junit'],\n"
            f"  #   junitReporter: {{ outputDir: '{self._JUNIT_OUTPUT_DIR}', "
            "outputFile: 'results.xml', useBrowserName: false },\n"
            "then re-run."
        )
        if "karma-junit-reporter" not in _package_deps(pkg):
            raise ToolNotAvailableError(install_hint)
        karma_conf = os.path.join(working_dir, "karma.conf.js")
        conf_text = ""
        if os.path.isfile(karma_conf):
            with open(karma_conf, encoding="utf-8") as f:
                conf_text = f.read()
        if "junit" not in conf_text:
            raise ToolNotAvailableError(install_hint)

    def verify_build(self, working_dir: str) -> BuildResult:
        # `test`, not a heavier `build`/e2e task -- same reasoning as
        # Gradle's own verify_build: skip heavier/flakier tasks, this is
        # the correctness gate.
        result = _run(
            ["npx", "ng", "test", "--no-watch", "--no-progress", "--browsers=ChromeHeadless"],
            cwd=working_dir, timeout=1800,
        )
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        args = ["npx", "ng", "test", "--no-watch", "--no-progress", "--browsers=ChromeHeadless"]
        for t in test_classes:
            args.append(f"--include={t}")
        result = _run(args, cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))


class TypeScriptVitestAdapter(_AngularAdapter):
    """Vitest ships a built-in `junit` reporter, invoked directly via CLI
    flags -- unlike Karma, no external reporter package or target-repo
    config change is ever needed for verify_build/run_specific_tests to
    produce a JUnit-XML report."""

    _JUNIT_OUTPUT_FILE = "test-results/vitest/results.xml"

    def preflight_check(self, working_dir: str) -> None:
        missing = self._missing_node_tools()
        if missing:
            raise ToolNotAvailableError(
                f"Cannot build this project: missing required tool(s): {', '.join(missing)}. "
                f"Install Node.js (https://nodejs.org) and ensure it's on PATH, then re-run."
            )
        pkg = _read_package_json(working_dir)
        if pkg is None:
            raise ToolNotAvailableError(
                f"No valid package.json found at {working_dir} — not an npm project, "
                f"or its package.json isn't valid JSON."
            )
        if "vitest" not in _package_deps(pkg):
            raise ToolNotAvailableError(
                f"vitest is not a dependency in {working_dir}/package.json — this adapter "
                f"was resolved for a Vitest project but none is installed here."
            )

    def verify_build(self, working_dir: str) -> BuildResult:
        result = _run(
            ["npx", "vitest", "run", "--reporter=junit", f"--outputFile={self._JUNIT_OUTPUT_FILE}"],
            cwd=working_dir, timeout=1800,
        )
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))

    def run_specific_tests(self, working_dir: str, test_classes: list[str]) -> BuildResult:
        args = [
            "npx", "vitest", "run", "--reporter=junit", f"--outputFile={self._JUNIT_OUTPUT_FILE}",
            *test_classes,
        ]
        result = _run(args, cwd=working_dir, timeout=300)
        return BuildResult(passed=result.returncode == 0, errors=_combined_output(result))


ADAPTER_REGISTRY = {
    "java-maven": JavaMavenAdapter,
    "java-gradle": JavaGradleAdapter,
    "typescript-karma": TypeScriptKarmaAdapter,
    "typescript-vitest": TypeScriptVitestAdapter,
    # "typescript-jest": TypeScriptJestAdapter,  # add later, once verified
    #                                             # against a real Jest project
    # "python": PythonAdapter,        # add later
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


def detect_test_runner(working_dir: str) -> str:
    """Inspects package.json's dependencies for 'karma' vs 'vitest' and
    returns 'typescript-karma' or 'typescript-vitest'. Same "fail clearly,
    don't guess" contract as detect_build_tool() for Java: raises
    BuildToolNotDetectedError on no package.json, both present, or
    neither."""
    pkg = _read_package_json(working_dir)
    if pkg is None:
        raise BuildToolNotDetectedError(
            f"No valid package.json found at {working_dir} — can't determine the "
            f"TypeScript test runner. Set LANGUAGE explicitly in .env if this "
            f"is a non-standard layout."
        )
    deps = _package_deps(pkg)
    has_karma = "karma" in deps
    has_vitest = "vitest" in deps
    if has_karma and has_vitest:
        raise BuildToolNotDetectedError(
            f"Both karma and vitest are dependencies in {working_dir}/package.json — "
            f"ambiguous. Set LANGUAGE explicitly to 'typescript-karma' or "
            f"'typescript-vitest' in .env."
        )
    if has_karma:
        return "typescript-karma"
    if has_vitest:
        return "typescript-vitest"
    raise BuildToolNotDetectedError(
        f"Neither karma nor vitest found in {working_dir}/package.json's "
        f"dependencies — can't determine the TypeScript test runner. Set "
        f"LANGUAGE explicitly in .env if this is a non-standard layout."
    )


def get_adapter(language: str, working_dir: str | None = None) -> LanguageAdapter:
    """language == 'java' triggers auto-detection against working_dir
    (Maven vs Gradle); language == 'typescript' triggers auto-detection
    too (Karma vs Vitest). Anything else (e.g. explicit 'java-maven'/
    'typescript-vitest') is used as-is — an explicit LANGUAGE in .env
    always wins over detection, which matters for the ambiguous
    mixed-tooling cases both detectors can hit."""
    resolved = language
    if language == "java":
        if working_dir is None:
            raise ValueError("working_dir is required to auto-detect a 'java' project's build tool")
        resolved = detect_build_tool(working_dir)
    elif language == "typescript":
        if working_dir is None:
            raise ValueError("working_dir is required to auto-detect a 'typescript' project's test runner")
        resolved = detect_test_runner(working_dir)

    if resolved not in ADAPTER_REGISTRY:
        raise ValueError(f"No LanguageAdapter registered for '{resolved}'")
    return ADAPTER_REGISTRY[resolved]()

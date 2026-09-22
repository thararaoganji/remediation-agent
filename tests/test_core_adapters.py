import subprocess

import pytest

from core.adapters import base


# --- _combined_output --------------------------------------------------

def test_combined_output_includes_stdout_even_when_stderr_nonempty():
    """Regression: `result.stderr or result.stdout` picked stderr whenever
    it was non-empty AT ALL -- including when stderr is just JVM startup
    warnings (native access, deprecated Unsafe, final-field-mutation --
    all extremely common on modern JDKs), silently discarding stdout even
    when it held the actual Maven/Gradle [ERROR] failure summary.
    Confirmed live: a real `mvn sonar:sonar` failure surfaced as nothing
    but JVM warning noise with zero information about the actual cause."""
    result = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout="[ERROR] Failed to execute goal ...\nBUILD FAILURE",
        stderr="WARNING: A restricted method in java.lang.System has been called",
    )
    combined = base._combined_output(result)
    assert "BUILD FAILURE" in combined
    assert "restricted method" in combined


def test_combined_output_handles_empty_streams():
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    assert base._combined_output(result) == ""


def test_combined_output_stdout_only():
    result = subprocess.CompletedProcess(args=[], returncode=1, stdout="[ERROR] boom", stderr="")
    assert base._combined_output(result) == "[ERROR] boom"


# --- detect_build_tool --------------------------------------------------

def test_detect_build_tool_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert base.detect_build_tool(str(tmp_path)) == "java-maven"


def test_detect_build_tool_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    assert base.detect_build_tool(str(tmp_path)) == "java-gradle"


def test_detect_build_tool_gradle_kts(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    assert base.detect_build_tool(str(tmp_path)) == "java-gradle"


def test_detect_build_tool_ambiguous_raises(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("")
    with pytest.raises(base.BuildToolNotDetectedError):
        base.detect_build_tool(str(tmp_path))


def test_detect_build_tool_neither_raises(tmp_path):
    with pytest.raises(base.BuildToolNotDetectedError):
        base.detect_build_tool(str(tmp_path))


def test_get_adapter_explicit_language_skips_detection():
    # explicit resolved name shouldn't need working_dir/detection at all
    adapter = base.get_adapter("java-maven")
    assert isinstance(adapter, base.JavaMavenAdapter)


def test_get_adapter_java_autodetects(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    adapter = base.get_adapter("java", str(tmp_path))
    assert isinstance(adapter, base.JavaGradleAdapter)


def test_get_adapter_unknown_language_raises():
    with pytest.raises(ValueError):
        base.get_adapter("python")


# --- preflight_check (tool discovery) ---------------------------------------

def test_maven_preflight_uses_wrapper_when_present(tmp_path):
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    (tmp_path / "mvnw.cmd").write_text("@echo off\n")
    adapter = base.JavaMavenAdapter()
    # should not raise: wrapper satisfies the "mvn on PATH" requirement
    # regardless of whether a real `mvn` binary exists on this machine
    adapter.preflight_check(str(tmp_path))


def test_maven_preflight_raises_when_neither_wrapper_nor_mvn(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: name == "java")
    adapter = base.JavaMavenAdapter()
    with pytest.raises(base.ToolNotAvailableError):
        adapter.preflight_check(str(tmp_path))


def test_gradle_preflight_wrapper_without_jar_reports_specific_reason(tmp_path, monkeypatch):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "gradlew.bat").write_text("@echo off\n")
    monkeypatch.setattr(base, "_tool_on_path", lambda name: name == "java")
    adapter = base.JavaGradleAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="gradle-wrapper.jar is missing"):
        adapter.preflight_check(str(tmp_path))


# --- Java test_identifier / production_to_test_path -------------------------

def test_java_fqcn_strips_source_root_prefix():
    assert base._java_fqcn("src/test/java/portal/expenses/controller/AuthControllerTest.java") \
        == "portal.expenses.controller.AuthControllerTest"


def test_java_fqcn_handles_main_source_root():
    assert base._java_fqcn("src/main/java/portal/expenses/service/ExpenseService.java") \
        == "portal.expenses.service.ExpenseService"


def test_java_maven_adapter_test_identifier_delegates_to_java_fqcn():
    adapter = base.JavaMavenAdapter()
    assert adapter.test_identifier("src/test/java/a/b/FooTest.java") == "a.b.FooTest"


def test_java_gradle_adapter_production_to_test_path():
    adapter = base.JavaGradleAdapter()
    assert adapter.production_to_test_path("src/main/java/a/b/Foo.java") == "src/test/java/a/b/FooTest.java"


def test_java_adapters_get_fix_prompt_addendum_is_nonempty_and_shared():
    """Regression: both Java adapters used to return "" here -- the actual
    Java/Spring guidance text (JAVA_SPRING_ADDENDUM) was defined in
    agent_techdebt/prompts.py but never wired into build_fix_prompt's
    language_addendum, so it silently never reached the LLM in production.
    Moved onto the adapters themselves so every one of the 3 agents that
    calls get_fix_prompt_addendum() actually gets it."""
    maven_text = base.JavaMavenAdapter().get_fix_prompt_addendum()
    gradle_text = base.JavaGradleAdapter().get_fix_prompt_addendum()
    assert maven_text == gradle_text
    assert "S6809" in maven_text


# --- detect_test_runner / TypeScript adapter registration --------------------

def _write_package_json(tmp_path, deps=None, dev_deps=None):
    import json
    pkg = {"name": "x", "dependencies": deps or {}, "devDependencies": dev_deps or {}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))


def test_detect_test_runner_karma(tmp_path):
    _write_package_json(tmp_path, dev_deps={"karma": "^6.4.0"})
    assert base.detect_test_runner(str(tmp_path)) == "typescript-karma"


def test_detect_test_runner_vitest(tmp_path):
    _write_package_json(tmp_path, dev_deps={"vitest": "^2.0.0"})
    assert base.detect_test_runner(str(tmp_path)) == "typescript-vitest"


def test_detect_test_runner_both_raises_ambiguous(tmp_path):
    _write_package_json(tmp_path, dev_deps={"karma": "^6.4.0", "vitest": "^2.0.0"})
    with pytest.raises(base.BuildToolNotDetectedError, match="[Aa]mbiguous"):
        base.detect_test_runner(str(tmp_path))


def test_detect_test_runner_neither_raises(tmp_path):
    _write_package_json(tmp_path)
    with pytest.raises(base.BuildToolNotDetectedError):
        base.detect_test_runner(str(tmp_path))


def test_detect_test_runner_no_package_json_raises(tmp_path):
    with pytest.raises(base.BuildToolNotDetectedError):
        base.detect_test_runner(str(tmp_path))


def test_get_adapter_typescript_autodetects_vitest(tmp_path):
    _write_package_json(tmp_path, dev_deps={"vitest": "^2.0.0"})
    adapter = base.get_adapter("typescript", str(tmp_path))
    assert isinstance(adapter, base.TypeScriptVitestAdapter)


def test_get_adapter_typescript_explicit_karma_skips_detection():
    adapter = base.get_adapter("typescript-karma")
    assert isinstance(adapter, base.TypeScriptKarmaAdapter)


# --- Angular production_to_test_path / test_identifier ----------------------

def test_angular_production_to_test_path_component():
    assert base._angular_production_to_test_path("src/app/login/login.component.ts") \
        == "src/app/login/login.component.spec.ts"


def test_angular_production_to_test_path_leaves_spec_unchanged():
    assert base._angular_production_to_test_path("src/app/login/login.component.spec.ts") \
        == "src/app/login/login.component.spec.ts"


def test_typescript_adapter_test_identifier_returns_path_unchanged():
    adapter = base.TypeScriptKarmaAdapter()
    assert adapter.test_identifier("src/app/login/login.component.spec.ts") \
        == "src/app/login/login.component.spec.ts"


# --- TypeScriptKarmaAdapter.preflight_check ----------------------------------

def test_karma_preflight_raises_when_node_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: False)
    adapter = base.TypeScriptKarmaAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="node"):
        adapter.preflight_check(str(tmp_path))


def test_karma_preflight_raises_when_no_package_json(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    adapter = base.TypeScriptKarmaAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="package.json"):
        adapter.preflight_check(str(tmp_path))


def test_karma_preflight_raises_when_junit_reporter_not_a_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    _write_package_json(tmp_path, dev_deps={"karma": "^6.4.0"})
    adapter = base.TypeScriptKarmaAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="karma-junit-reporter"):
        adapter.preflight_check(str(tmp_path))


def test_karma_preflight_raises_when_dependency_present_but_not_wired_into_config(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    _write_package_json(tmp_path, dev_deps={"karma": "^6.4.0", "karma-junit-reporter": "^2.0.0"})
    (tmp_path / "karma.conf.js").write_text("module.exports = function(config) { config.set({}); };")
    adapter = base.TypeScriptKarmaAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="karma-junit-reporter"):
        adapter.preflight_check(str(tmp_path))


def test_karma_preflight_passes_when_reporter_installed_and_wired(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    _write_package_json(tmp_path, dev_deps={"karma": "^6.4.0", "karma-junit-reporter": "^2.0.0"})
    (tmp_path / "karma.conf.js").write_text(
        "module.exports = function(config) { config.set({ reporters: ['progress', 'junit'] }); };"
    )
    adapter = base.TypeScriptKarmaAdapter()
    adapter.preflight_check(str(tmp_path))  # should not raise


# --- TypeScriptVitestAdapter.preflight_check ---------------------------------

def test_vitest_preflight_raises_when_vitest_not_a_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    _write_package_json(tmp_path)
    adapter = base.TypeScriptVitestAdapter()
    with pytest.raises(base.ToolNotAvailableError, match="vitest"):
        adapter.preflight_check(str(tmp_path))


def test_vitest_preflight_passes_when_vitest_is_a_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_tool_on_path", lambda name: True)
    _write_package_json(tmp_path, dev_deps={"vitest": "^2.0.0"})
    adapter = base.TypeScriptVitestAdapter()
    adapter.preflight_check(str(tmp_path))  # should not raise


# --- _AngularAdapter.prepare_workspace ---------------------------------------

def test_angular_prepare_workspace_runs_npm_ci(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, cwd=None, timeout=None, env=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(base, "_run", fake_run)
    base.TypeScriptKarmaAdapter().prepare_workspace(str(tmp_path))
    assert captured["args"] == ["npm", "ci"]


def test_angular_prepare_workspace_raises_on_failure(tmp_path, monkeypatch):
    def fake_run(args, cwd=None, timeout=None, env=None):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="npm ERR!")

    monkeypatch.setattr(base, "_run", fake_run)
    with pytest.raises(base.ToolNotAvailableError, match="npm ci"):
        base.TypeScriptKarmaAdapter().prepare_workspace(str(tmp_path))

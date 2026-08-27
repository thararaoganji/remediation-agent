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

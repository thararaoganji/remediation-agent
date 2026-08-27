import subprocess

import pytest

from sonar import adapters


# --- SonarJavaMavenAdapter.run_sonar_scan -------------------------------------

def test_maven_run_sonar_scan_uses_fully_qualified_goal_not_prefix(tmp_path, monkeypatch):
    """Regression: the "sonar:sonar" prefix shorthand only resolves if
    org.sonarsource.scanner.maven is registered in this Maven install's
    ~/.m2/settings.xml <pluginGroups>, or the target project's own pom.xml
    already declares sonar-maven-plugin as a build plugin. Neither holds
    for a repo that's never had Sonar wired into its POM -- confirmed
    live: WebGoat's first-ever checkpoint scan failed outright with
    "No plugin found for prefix 'sonar'", crashing the whole run. The
    fully-qualified groupId:artifactId:goal form resolves directly from
    the repository regardless of either."""
    captured = {}

    def fake_run(args, cwd=None, env=None, timeout=None):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args=args, returncode=0,
            stdout="[INFO] More about the report processing at "
                   "http://localhost:9000/api/ce/task?id=abc123",
            stderr="",
        )

    monkeypatch.setattr(adapters, "_run", fake_run)
    adapter = adapters.SonarJavaMavenAdapter()
    task_id = adapter.run_sonar_scan(str(tmp_path), "http://localhost:9000", "tok", "my:proj")

    assert task_id == "abc123"
    assert "org.sonarsource.scanner.maven:sonar-maven-plugin:sonar" in captured["args"]
    assert "sonar:sonar" not in captured["args"]


def test_maven_run_sonar_scan_raises_with_output_on_failure(tmp_path, monkeypatch):
    def fake_run(args, cwd=None, env=None, timeout=None):
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="[ERROR] No plugin found for prefix 'sonar'", stderr="",
        )

    monkeypatch.setattr(adapters, "_run", fake_run)
    adapter = adapters.SonarJavaMavenAdapter()
    with pytest.raises(RuntimeError, match="No plugin found for prefix"):
        adapter.run_sonar_scan(str(tmp_path), "http://localhost:9000", "tok", "my:proj")


# --- SonarJavaMavenAdapter.get_project_key ---------------------------------------

def test_maven_get_project_key_explicit_property(tmp_path):
    (tmp_path / "pom.xml").write_text("""
    <project xmlns="http://maven.apache.org/POM/4.0.0">
      <groupId>com.example</groupId>
      <artifactId>myapp</artifactId>
      <properties><sonar.projectKey>my-custom-key</sonar.projectKey></properties>
    </project>
    """)
    adapter = adapters.SonarJavaMavenAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "my-custom-key"


def test_maven_get_project_key_falls_back_to_group_artifact(tmp_path):
    (tmp_path / "pom.xml").write_text("""
    <project xmlns="http://maven.apache.org/POM/4.0.0">
      <groupId>org.owasp.webgoat</groupId>
      <artifactId>webgoat</artifactId>
    </project>
    """)
    adapter = adapters.SonarJavaMavenAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "org.owasp.webgoat:webgoat"


def test_maven_get_project_key_group_id_from_parent(tmp_path):
    (tmp_path / "pom.xml").write_text("""
    <project xmlns="http://maven.apache.org/POM/4.0.0">
      <parent><groupId>org.example.parent</groupId></parent>
      <artifactId>child-module</artifactId>
    </project>
    """)
    adapter = adapters.SonarJavaMavenAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "org.example.parent:child-module"


def test_maven_get_project_key_no_pom_raises(tmp_path):
    adapter = adapters.SonarJavaMavenAdapter()
    with pytest.raises(adapters.SonarConfigNotFoundError):
        adapter.get_project_key(str(tmp_path))


def test_maven_get_project_key_invalid_xml_raises(tmp_path):
    (tmp_path / "pom.xml").write_text("<not-valid-xml")
    adapter = adapters.SonarJavaMavenAdapter()
    with pytest.raises(adapters.SonarConfigNotFoundError):
        adapter.get_project_key(str(tmp_path))


def test_maven_get_project_key_no_group_or_artifact_raises(tmp_path):
    (tmp_path / "pom.xml").write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"></project>')
    adapter = adapters.SonarJavaMavenAdapter()
    with pytest.raises(adapters.SonarConfigNotFoundError):
        adapter.get_project_key(str(tmp_path))


# --- SonarJavaGradleAdapter.get_project_key --------------------------------------

def test_gradle_get_project_key_from_groovy_dsl(tmp_path):
    (tmp_path / "build.gradle").write_text("""
    sonar {
      properties {
        property "sonar.projectKey", "my-gradle-key"
      }
    }
    """)
    adapter = adapters.SonarJavaGradleAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "my-gradle-key"


def test_gradle_get_project_key_from_kotlin_dsl(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("""
    sonar {
        property("sonar.projectKey", "kotlin-dsl-key")
    }
    """)
    adapter = adapters.SonarJavaGradleAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "kotlin-dsl-key"


def test_gradle_get_project_key_from_properties_file(tmp_path):
    (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
    (tmp_path / "gradle.properties").write_text("systemProp.sonar.projectKey=props-file-key\n")
    adapter = adapters.SonarJavaGradleAdapter()
    assert adapter.get_project_key(str(tmp_path)) == "props-file-key"


def test_gradle_get_project_key_no_build_file_raises(tmp_path):
    adapter = adapters.SonarJavaGradleAdapter()
    with pytest.raises(adapters.SonarConfigNotFoundError):
        adapter.get_project_key(str(tmp_path))


def test_gradle_get_project_key_build_file_present_but_no_key_raises(tmp_path):
    (tmp_path / "build.gradle").write_text("apply plugin: 'java'")
    adapter = adapters.SonarJavaGradleAdapter()
    with pytest.raises(adapters.SonarConfigNotFoundError):
        adapter.get_project_key(str(tmp_path))

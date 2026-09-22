import os

import pytest
import requests

from core.adapters import jdk_provisioning as jp


# --- _normalize_java_version --------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("17", 17),
    ("21", 21),
    ("1.8", 8),
    ("1.7", 7),
    ("VERSION_17", 17),
    ("VERSION_1_8", 8),
    ("'17'", 17),
    ('"11"', 11),
    ("not-a-version", None),
])
def test_normalize_java_version(raw, expected):
    assert jp._normalize_java_version(raw) == expected


# --- detect_java_version: Maven -----------------------------------------

def test_detect_java_version_maven_release_property(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"
    )
    assert jp.detect_java_version(str(tmp_path), "java-maven") == 17


def test_detect_java_version_maven_old_style_source_target(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties>"
        "<maven.compiler.source>1.8</maven.compiler.source>"
        "<maven.compiler.target>1.8</maven.compiler.target>"
        "</properties></project>"
    )
    assert jp.detect_java_version(str(tmp_path), "java-maven") == 8


def test_detect_java_version_maven_spring_boot_java_version_property(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><java.version>11</java.version></properties></project>"
    )
    assert jp.detect_java_version(str(tmp_path), "java-maven") == 11


def test_detect_java_version_maven_compiler_plugin_configuration(tmp_path):
    (tmp_path / "pom.xml").write_text("""
        <project>
          <build><plugins><plugin>
            <artifactId>maven-compiler-plugin</artifactId>
            <configuration><release>21</release></configuration>
          </plugin></plugins></build>
        </project>
    """)
    assert jp.detect_java_version(str(tmp_path), "java-maven") == 21


def test_detect_java_version_maven_with_namespace(tmp_path):
    (tmp_path / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<properties><maven.compiler.release>17</maven.compiler.release></properties>"
        "</project>"
    )
    assert jp.detect_java_version(str(tmp_path), "java-maven") == 17


def test_detect_java_version_maven_no_pom_returns_none(tmp_path):
    assert jp.detect_java_version(str(tmp_path), "java-maven") is None


def test_detect_java_version_maven_no_version_declared_returns_none(tmp_path):
    (tmp_path / "pom.xml").write_text("<project><groupId>g</groupId><artifactId>a</artifactId></project>")
    assert jp.detect_java_version(str(tmp_path), "java-maven") is None


def test_detect_java_version_maven_invalid_xml_returns_none(tmp_path):
    (tmp_path / "pom.xml").write_text("not xml at all <<<")
    assert jp.detect_java_version(str(tmp_path), "java-maven") is None


# --- detect_java_version: Gradle ----------------------------------------

def test_detect_java_version_gradle_source_compatibility_groovy(tmp_path):
    (tmp_path / "build.gradle").write_text("sourceCompatibility = '17'\n")
    assert jp.detect_java_version(str(tmp_path), "java-gradle") == 17


def test_detect_java_version_gradle_source_compatibility_enum(tmp_path):
    (tmp_path / "build.gradle").write_text("sourceCompatibility = JavaVersion.VERSION_11\n")
    assert jp.detect_java_version(str(tmp_path), "java-gradle") == 11


def test_detect_java_version_gradle_old_style_1_8(tmp_path):
    (tmp_path / "build.gradle").write_text("sourceCompatibility = 1.8\n")
    assert jp.detect_java_version(str(tmp_path), "java-gradle") == 8


def test_detect_java_version_gradle_toolchain_kotlin_dsl(tmp_path):
    (tmp_path / "build.gradle.kts").write_text(
        "java {\n    toolchain {\n        languageVersion.set(JavaLanguageVersion.of(21))\n    }\n}\n"
    )
    assert jp.detect_java_version(str(tmp_path), "java-gradle") == 21


def test_detect_java_version_gradle_toolchain_wins_over_compatibility(tmp_path):
    # Toolchain is the modern, more specific mechanism -- checked first when
    # a project (unusually) sets both.
    (tmp_path / "build.gradle").write_text(
        "sourceCompatibility = '11'\n"
        "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }\n"
    )
    assert jp.detect_java_version(str(tmp_path), "java-gradle") == 21


def test_detect_java_version_gradle_no_build_file_returns_none(tmp_path):
    assert jp.detect_java_version(str(tmp_path), "java-gradle") is None


def test_detect_java_version_gradle_no_version_declared_returns_none(tmp_path):
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    assert jp.detect_java_version(str(tmp_path), "java-gradle") is None


# --- _jdk_home_for_version -----------------------------------------------

def test_jdk_home_for_version_uses_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_HOME_17", str(tmp_path))
    assert jp._jdk_home_for_version(17) == str(tmp_path)


def test_jdk_home_for_version_missing_returns_none(monkeypatch):
    monkeypatch.delenv("JAVA_HOME_99", raising=False)
    monkeypatch.setattr(jp, "_JDK_PATH_CANDIDATES", [])
    assert jp._jdk_home_for_version(99) is None


def test_jdk_home_for_version_checks_download_cache_dir(tmp_path, monkeypatch):
    cache_dir = tmp_path / "jdk-cache"
    version_dir = cache_dir / "17"
    version_dir.mkdir(parents=True)
    monkeypatch.delenv("JAVA_HOME_17", raising=False)
    monkeypatch.setattr(jp, "_JDK_PATH_CANDIDATES", [os.path.join(str(cache_dir), "{version}")])
    assert jp._jdk_home_for_version(17) == str(version_dir)


# --- ensure_jdk_home ------------------------------------------------------

def test_ensure_jdk_home_returns_existing_without_downloading(monkeypatch):
    monkeypatch.setattr(jp, "_jdk_home_for_version", lambda v: "/already/there")

    def fail_download(v, timeout=300):
        raise AssertionError("should not download when already installed")

    monkeypatch.setattr(jp, "_download_and_install_jdk", fail_download)
    assert jp.ensure_jdk_home(17) == "/already/there"


def test_ensure_jdk_home_downloads_on_miss(monkeypatch):
    monkeypatch.setattr(jp, "_jdk_home_for_version", lambda v: None)
    monkeypatch.setattr(jp, "_download_and_install_jdk", lambda v, timeout=300: "/downloaded/17")
    assert jp.ensure_jdk_home(17) == "/downloaded/17"


# --- _adoptium_os_arch -----------------------------------------------------

def test_adoptium_os_arch_maps_linux_x86_64(monkeypatch):
    monkeypatch.setattr(jp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(jp.platform, "machine", lambda: "x86_64")
    assert jp._adoptium_os_arch() == ("linux", "x64")


def test_adoptium_os_arch_maps_darwin_arm64(monkeypatch):
    monkeypatch.setattr(jp.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(jp.platform, "machine", lambda: "arm64")
    assert jp._adoptium_os_arch() == ("mac", "aarch64")


def test_adoptium_os_arch_unsupported_returns_none(monkeypatch):
    monkeypatch.setattr(jp.platform, "system", lambda: "SunOS")
    monkeypatch.setattr(jp.platform, "machine", lambda: "sparc")
    assert jp._adoptium_os_arch() is None


# --- _download_and_install_jdk --------------------------------------------

def test_download_and_install_jdk_returns_none_on_unsupported_arch(monkeypatch):
    monkeypatch.setattr(jp, "_adoptium_os_arch", lambda: None)
    assert jp._download_and_install_jdk(17) is None


def test_download_and_install_jdk_returns_none_on_network_failure(monkeypatch):
    monkeypatch.setattr(jp, "_adoptium_os_arch", lambda: ("linux", "x64"))

    def fake_get(*a, **kw):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(jp.requests, "get", fake_get)
    assert jp._download_and_install_jdk(17) is None


def test_download_and_install_jdk_extracts_and_caches(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(jp, "_JDK_DOWNLOAD_CACHE_DIR", str(cache_dir))
    # _JDK_PATH_CANDIDATES is built from _JDK_DOWNLOAD_CACHE_DIR at import
    # time, so patching the latter alone doesn't move the former -- needed
    # here since _jdk_home_for_version() (used below, via ensure_jdk_home())
    # reads _JDK_PATH_CANDIDATES, not _JDK_DOWNLOAD_CACHE_DIR directly.
    monkeypatch.setattr(jp, "_JDK_PATH_CANDIDATES", [os.path.join(str(cache_dir), "{version}")])
    monkeypatch.setattr(jp, "_adoptium_os_arch", lambda: ("linux", "x64"))

    # Build a real tar.gz containing a single top-level "jdk-17+9" dir with
    # a bin/java file inside, mirroring Adoptium's actual archive shape.
    import io
    import tarfile as tarfile_mod

    archive_bytes = io.BytesIO()
    with tarfile_mod.open(fileobj=archive_bytes, mode="w:gz") as tf:
        info = tarfile_mod.TarInfo(name="jdk-17+9/bin/java")
        payload = b"fake java binary"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    archive_bytes.seek(0)
    body = archive_bytes.read()

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield body

    monkeypatch.setattr(jp.requests, "get", lambda *a, **kw: _FakeResponse())

    result = jp._download_and_install_jdk(17)
    assert result == str(cache_dir / "17")
    assert os.path.isfile(os.path.join(result, "bin", "java"))

    # ensure_jdk_home() is the layer responsible for not re-downloading once
    # cached (it checks _jdk_home_for_version() first) -- confirm that
    # holds now that the version is actually on disk under the real cache
    # dir (not a mocked _jdk_home_for_version like the other ensure_jdk_home
    # tests use).
    def fail_get(*a, **kw):
        raise AssertionError("should not re-download once cached")

    monkeypatch.setattr(jp.requests, "get", fail_get)
    assert jp.ensure_jdk_home(17) == str(cache_dir / "17")


def test_download_and_install_jdk_returns_none_on_malformed_archive(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(jp, "_JDK_DOWNLOAD_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(jp, "_adoptium_os_arch", lambda: ("linux", "x64"))

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"not actually a gzip tarball"

    monkeypatch.setattr(jp.requests, "get", lambda *a, **kw: _FakeResponse())
    assert jp._download_and_install_jdk(17) is None


# --- resolve_java_env ------------------------------------------------------

def test_resolve_java_env_none_when_no_version_detected(tmp_path):
    assert jp.resolve_java_env(str(tmp_path), "java-maven") is None


def test_resolve_java_env_none_when_version_cannot_be_provisioned(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>99</maven.compiler.release></properties></project>"
    )
    monkeypatch.setattr(jp, "ensure_jdk_home", lambda v: None)
    assert jp.resolve_java_env(str(tmp_path), "java-maven") is None


def test_resolve_java_env_sets_java_home_and_prepends_path(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"
    )
    fake_home = str(tmp_path / "jdk-17")
    monkeypatch.setattr(jp, "ensure_jdk_home", lambda v: fake_home)
    env = jp.resolve_java_env(str(tmp_path), "java-maven")
    assert env["JAVA_HOME"] == fake_home
    assert env["PATH"].startswith(os.path.join(fake_home, "bin") + os.pathsep)


# --- describe_java_selection ------------------------------------------------

def test_describe_java_selection_no_version_declared(tmp_path):
    assert "no explicit Java version" in jp.describe_java_selection(str(tmp_path), "java-maven")


def test_describe_java_selection_already_installed(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"
    )
    monkeypatch.setattr(jp, "_jdk_home_for_version", lambda v: "/opt/jdk-17")
    monkeypatch.setattr(jp, "ensure_jdk_home", lambda v: "/opt/jdk-17")
    note = jp.describe_java_selection(str(tmp_path), "java-maven")
    assert "Java 17" in note and "/opt/jdk-17" in note and "downloaded" not in note


def test_describe_java_selection_downloaded(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"
    )
    monkeypatch.setattr(jp, "_jdk_home_for_version", lambda v: None)
    monkeypatch.setattr(jp, "ensure_jdk_home", lambda v: "/tmp/sonar_remediation_jdks/17")
    note = jp.describe_java_selection(str(tmp_path), "java-maven")
    assert "downloaded" in note and "Java 17" in note


def test_describe_java_selection_cannot_provision(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(
        "<project><properties><maven.compiler.release>99</maven.compiler.release></properties></project>"
    )
    monkeypatch.setattr(jp, "_jdk_home_for_version", lambda v: None)
    monkeypatch.setattr(jp, "ensure_jdk_home", lambda v: None)
    note = jp.describe_java_selection(str(tmp_path), "java-maven")
    assert "couldn't get a matching JDK" in note

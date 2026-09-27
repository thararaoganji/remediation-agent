"""
Best-effort Java-version detection and on-demand JDK provisioning, used by
core.adapters.base's LanguageAdapter subclasses to build/test/scan a
project under the JDK its own pom.xml/build.gradle actually declares,
instead of always whatever `java` happens to be on PATH.

Kept in its own module rather than folded into core.adapters.base:
detection is pure build-file parsing, but provisioning does real network
I/O (downloading a JDK from Adoptium's own API when the declared version
isn't already installed) -- deliberately NOT baked into the Docker image
as a bundle of every JDK version, since Cloud Run Jobs pull the image
fresh on every execution and that size cost would land on every single
run, not once. Downloading only the one version actually needed, once per
version per container, is the better tradeoff here.

Every public function degrades to None/no-op on failure rather than
raising -- a project whose declared version can't be detected, or can't be
provisioned (offline, unsupported OS/arch, a version Adoptium doesn't
publish), still gets built, just under whatever JDK was already on PATH.
That's an existing, understood fallback, not a new failure mode.
"""

import os
import platform
import re
import shutil
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import requests


def _normalize_java_version(raw: str) -> int | None:
    """'1.8' / '1_8' / 'VERSION_1_8' -> 8, '17' / 'VERSION_17' -> 17. Both
    styles show up in the wild: pre-Java-9 poms/build files use the old
    '1.x' scheme, everything since uses the bare major version."""
    raw = raw.strip().strip("'\"")
    if raw.upper().startswith("VERSION_"):
        raw = raw[len("VERSION_"):]
    raw = raw.replace("_", ".")
    if raw.startswith("1.") and raw[2:].isdigit():
        return int(raw[2:])
    if raw.isdigit():
        return int(raw)
    return None


def _maven_pom_java_version(working_dir: str) -> int | None:
    pom_path = os.path.join(working_dir, "pom.xml")
    if not os.path.isfile(pom_path):
        return None
    try:
        root = ET.parse(pom_path).getroot()
    except ET.ParseError:
        return None
    # pom.xml's default namespace makes every find need the ns prefix
    # explicitly, or ElementTree silently returns nothing -- same gotcha
    # sonar/adapters.py's get_project_key() works around.
    ns = {"m": "http://maven.apache.org/POM/4.0.0"} if root.tag.startswith("{") else {}

    def findtext(path: str) -> str | None:
        tag = "/".join(f"m:{seg}" for seg in path.split("/")) if ns else path
        return root.findtext(tag, namespaces=ns)

    # maven.compiler.release (Java 9+) is a single authoritative flag that
    # replaces source+target together -- checked first as the strongest
    # signal when a project sets it. java.version is Spring Boot's own
    # parent-POM convention, checked last as the weakest/most indirect.
    for prop in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source", "java.version"):
        value = findtext(f"properties/{prop}")
        if value:
            version = _normalize_java_version(value)
            if version is not None:
                return version

    # Fall back to maven-compiler-plugin's own <configuration> block, for
    # projects that set release/source/target there instead of as a
    # top-level property.
    plugin_tag = "m:plugin" if ns else "plugin"
    for plugin in root.iterfind(f".//{plugin_tag}", ns):
        artifact_id = plugin.findtext("m:artifactId" if ns else "artifactId", namespaces=ns)
        if artifact_id != "maven-compiler-plugin":
            continue
        config = plugin.find("m:configuration" if ns else "configuration", ns)
        if config is None:
            continue
        for tag in ("release", "target", "source"):
            value = config.findtext(f"m:{tag}" if ns else tag, namespaces=ns)
            if value:
                version = _normalize_java_version(value)
                if version is not None:
                    return version
    return None


# Groovy: `sourceCompatibility = 17` / `sourceCompatibility JavaVersion.VERSION_17` / `sourceCompatibility = '17'`
# Kotlin DSL: `sourceCompatibility = JavaVersion.VERSION_17`
_GRADLE_COMPATIBILITY_RE = re.compile(
    r"(?:source|target)Compatibility\s*=?\s*(?:JavaVersion\.)?([\w.\"']+)"
)
# Toolchain API (both DSLs): `languageVersion.set(JavaLanguageVersion.of(17))` / `languageVersion = JavaLanguageVersion.of(17)`
_GRADLE_TOOLCHAIN_RE = re.compile(r"JavaLanguageVersion\.of\(\s*[\"']?(\d+)[\"']?\s*\)")


def _gradle_build_java_version(working_dir: str) -> int | None:
    for name in ("build.gradle.kts", "build.gradle"):
        path = os.path.join(working_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # Toolchain checked first -- it's the modern, more specific
        # mechanism and wins over source/targetCompatibility when a project
        # sets both (Gradle itself prefers the toolchain when present).
        m = _GRADLE_TOOLCHAIN_RE.search(text)
        if m:
            return int(m.group(1))
        m = _GRADLE_COMPATIBILITY_RE.search(text)
        if m:
            version = _normalize_java_version(m.group(1))
            if version is not None:
                return version
    return None


def detect_java_version(working_dir: str, build_tool: str) -> int | None:
    """Best-effort: the Java major version (e.g. 17) a project's own build
    file declares it compiles/targets for, or None if it can't be
    determined -- no explicit setting found, a version string this doesn't
    recognize, or (for Maven) a version only set on an unresolved parent
    POM this doesn't fetch. None means 'fall back to whatever java is
    already on PATH', the same behavior as before this existed."""
    if build_tool == "java-maven":
        return _maven_pom_java_version(working_dir)
    if build_tool == "java-gradle":
        return _gradle_build_java_version(working_dir)
    return None


# Where an on-demand download gets extracted to, and the first place
# checked before downloading again -- so a version downloaded once is
# reused for every later compile/verify/scan call in the same container
# (get_adapter() constructs a fresh adapter many times per run; there's no
# adapter-lifetime state to cache this on, see resolve_java_env() below).
_JDK_DOWNLOAD_CACHE_DIR = os.path.join(tempfile.gettempdir(), "sonar_remediation_jdks")

# JAVA_HOME_<N> is checked first -- the convention actions/setup-java uses
# in CI, in case a run ever happens there instead. /opt/jdks/<N> is a
# convention a custom base image could pre-bundle a version under (this
# repo's own Dockerfile doesn't, by design -- see this module's docstring).
# The Debian/Ubuntu package paths are what a non-Docker Debian/Ubuntu host
# would have from installing temurin-<N>-jdk / openjdk-<N>-jdk itself.
_JDK_HOME_ENV_TEMPLATE = "JAVA_HOME_{version}"
_JDK_PATH_CANDIDATES = [
    os.path.join(_JDK_DOWNLOAD_CACHE_DIR, "{version}"),
    "/opt/jdks/{version}",
    "/usr/lib/jvm/temurin-{version}-jdk-amd64",
    "/usr/lib/jvm/java-{version}-openjdk-amd64",
]


def _jdk_home_for_version(version: int) -> str | None:
    """Checks only for an ALREADY installed/downloaded JDK -- never
    triggers a download itself. See ensure_jdk_home() for the version that
    downloads on a miss."""
    env_var = os.environ.get(_JDK_HOME_ENV_TEMPLATE.format(version=version))
    if env_var and os.path.isdir(env_var):
        return env_var
    for template in _JDK_PATH_CANDIDATES:
        path = template.format(version=version)
        if os.path.isdir(path):
            return path
    return None


def _adoptium_os_arch() -> tuple[str, str] | None:
    """Maps this host to Adoptium's own os/arch naming (which differs from
    both platform.system()/platform.machine() and Debian's dpkg
    architecture names). None means this OS/arch isn't one Adoptium
    publishes binaries for (or isn't one this maps -- e.g. 32-bit)."""
    os_name = {"Linux": "linux", "Darwin": "mac", "Windows": "windows"}.get(platform.system())
    arch = {
        "x86_64": "x64", "amd64": "x64",
        "aarch64": "aarch64", "arm64": "aarch64",
    }.get(platform.machine().lower())
    if os_name is None or arch is None:
        return None
    return os_name, arch


def _download_and_install_jdk(version: int, timeout: int = 300) -> str | None:
    """Downloads and extracts an Eclipse Temurin JDK for this exact major
    version from Adoptium's own API (the same distribution as this repo's
    base image, so behavior stays consistent with whatever the image
    already ships). Returns None on ANY failure -- offline, this OS/arch
    isn't published, Adoptium doesn't have this version, disk full, a
    malformed archive -- since this is a convenience, never something
    worth failing the whole run over; the caller falls back to PATH's
    default JDK exactly as if this function didn't exist."""
    os_arch = _adoptium_os_arch()
    if os_arch is None:
        return None
    os_name, arch = os_arch
    is_zip = os_name == "windows"

    url = (
        f"https://api.adoptium.net/v3/binary/latest/{version}/ga/{os_name}/{arch}/"
        f"jdk/hotspot/normal/eclipse?project=jdk"
    )
    try:
        response = requests.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        return None

    target_dir = os.path.join(_JDK_DOWNLOAD_CACHE_DIR, str(version))
    os.makedirs(_JDK_DOWNLOAD_CACHE_DIR, exist_ok=True)
    staging_dir = tempfile.mkdtemp(prefix=f"jdk-{version}-", dir=_JDK_DOWNLOAD_CACHE_DIR)
    try:
        archive_path = os.path.join(staging_dir, "jdk.zip" if is_zip else "jdk.tar.gz")
        with open(archive_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)

        extract_dir = os.path.join(staging_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        if is_zip:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(extract_dir)

        # Adoptium's archives have exactly one top-level dir, e.g.
        # "jdk-25.0.1+9" -- anything else means this isn't the archive
        # shape expected, so bail rather than guess.
        entries = os.listdir(extract_dir)
        if len(entries) != 1:
            return None
        extracted_home = os.path.join(extract_dir, entries[0])
        # macOS's tarball nests the actual JDK home one level deeper.
        mac_home = os.path.join(extracted_home, "Contents", "Home")
        if os.path.isdir(mac_home):
            extracted_home = mac_home

        if not os.path.isdir(target_dir):
            shutil.move(extracted_home, target_dir)
        return target_dir if os.path.isdir(target_dir) else None
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return None
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def ensure_jdk_home(version: int) -> str | None:
    """Like _jdk_home_for_version(), but downloads from Adoptium on a miss
    instead of just reporting one. Safe to call repeatedly for the same
    version within one run/container -- a hit after the first call is just
    filesystem checks, no repeat download."""
    home = _jdk_home_for_version(version)
    if home:
        return home
    return _download_and_install_jdk(version)


def resolve_java_env(working_dir: str, build_tool: str) -> dict | None:
    """None means 'inherit the parent process's environment unchanged' --
    exactly today's behavior, whenever the project doesn't declare an
    explicit Java version or that version couldn't be provisioned (already
    installed or downloaded). Deliberately re-detects on every call rather
    than caching anywhere: a fresh adapter is constructed per pipeline step
    (see each agent's fix.py / checkpoint.py -- get_adapter() is called
    many times per run, never held onto across steps), so there's no
    adapter-lifetime state to cache this on; re-reading one build file is a
    few KB of text, and ensure_jdk_home() above is itself cheap after the
    first call for a given version."""
    version = detect_java_version(working_dir, build_tool)
    if version is None:
        return None
    java_home = ensure_jdk_home(version)
    if java_home is None:
        return None
    env = dict(os.environ)
    env["JAVA_HOME"] = java_home
    env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")
    return env


def describe_java_selection(working_dir: str, build_tool: str) -> str:
    """Human-readable one-liner for a setup step to surface -- which Java
    version (if any) was detected from the build file, and how (or
    whether) a matching JDK was made available to run it with. Calling
    this also triggers the download (via ensure_jdk_home()) if needed, so
    a setup step calling this once upfront pays that latency once, clearly,
    instead of it happening silently deep in the first compile call."""
    version = detect_java_version(working_dir, build_tool)
    if version is None:
        return "no explicit Java version declared in the build file — using the default JDK on PATH"
    already_installed = _jdk_home_for_version(version) is not None
    java_home = ensure_jdk_home(version)
    if java_home is None:
        return (
            f"detected Java {version} from the build file, but couldn't get a matching JDK "
            f"(not already installed, and downloading Temurin {version} from Adoptium failed or "
            f"this OS/architecture isn't supported) — falling back to the default JDK on PATH, "
            f"which may fail to compile if it's a different major version"
        )
    if already_installed:
        return f"detected Java {version} from the build file — using {java_home}"
    return f"detected Java {version} from the build file — downloaded Temurin {version} to {java_home}"

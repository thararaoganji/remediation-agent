"""
Tool-agnostic patch application + JUnit failure parsing. The
finding-shaped half of the original patch_tools.py (cluster classification,
per-issue verification) is Sonar-specific and lives in sonar/tools/patch_tools.py
instead — see that module's docstring.
"""

import glob
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def apply_diff(diff_text: str, working_dir: str, file_path: str) -> bool:
    """Applies an LLM-generated unified diff to file_path within working_dir
    via `git apply`. Tries progressively more tolerant flag combinations
    before giving up, since LLM-authored diffs don't always have perfectly
    formed hunk headers or context lines. `--include` scopes every attempt
    to file_path as a safety net — the fix prompt targets exactly one file,
    so a diff touching anything else indicates a malformed/hallucinated
    patch, not a legitimate multi-file change.

    Returns False (leaves the working tree untouched) on any failure —
    callers treat that as 'flag for manual review', not a hard stop. Never
    raises for a bad diff; only for real filesystem/git errors."""
    if not diff_text or not diff_text.strip():
        return False

    abs_path = os.path.join(working_dir, file_path)
    try:
        before = open(abs_path, "r", encoding="utf-8").read()
    except OSError:
        before = None

    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
        f.write(diff_text)
        diff_path = f.name

    try:
        attempts = [
            ["git", "apply", "--whitespace=fix", f"--include={file_path}"],
            ["git", "apply", "--whitespace=fix", "--recount", f"--include={file_path}"],
            ["git", "apply", "--whitespace=fix", "--recount", "-p0", f"--include={file_path}"],
        ]
        for args in attempts:
            check = subprocess.run(
                [*args, "--check", diff_path], cwd=working_dir, capture_output=True, text=True,
            )
            if check.returncode != 0:
                continue
            applied = subprocess.run(
                [*args, diff_path], cwd=working_dir, capture_output=True, text=True,
            )
            if applied.returncode != 0:
                continue
            try:
                after = open(abs_path, "r", encoding="utf-8").read()
            except OSError:
                after = None
            if after != before:
                return True
            # git exited 0 but nothing actually changed — e.g. a strip-level
            # mismatch under -p0 matched zero files against --include and
            # silently no-op'd. Not a real application; keep trying.
        return False
    finally:
        os.unlink(diff_path)


# build/test-results/test, target/surefire-reports: Gradle/Maven, one
# TEST-<FQCN>.xml file per class. test-results/karma, test-results/vitest:
# karma-junit-reporter / Vitest's built-in junit reporter, each configured
# (per the TypeScript adapters' preflight instructions) to write into these
# dedicated, agent-owned directories -- see _parse_junit_failures's shape
# handling below for why a single combined file works the same as many.
_JUNIT_REPORT_DIRS = (
    "build/test-results/test", "target/surefire-reports",
    "test-results/karma", "test-results/vitest",
)


def _parse_junit_failures(reports_dir: str, limit: int) -> list[str]:
    failures = []
    for xml_path in sorted(glob.glob(os.path.join(reports_dir, "*.xml"))):
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            continue
        # Surefire/Gradle write one <testsuite> root per class (testcase
        # elements as direct children). Karma's/Vitest's junit reporters
        # write a single combined file instead: a <testsuites> root
        # wrapping one <testsuite> per spec file/describe block. Handle
        # both shapes by walking every <testsuite> the file actually has.
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            classname = suite.get("name", os.path.basename(xml_path))
            for testcase in suite.findall("testcase"):
                node = testcase.find("failure")
                if node is None:
                    node = testcase.find("error")
                if node is None:
                    continue
                method = testcase.get("name", "?")
                message = (node.get("message") or node.text or "").strip().splitlines()[0][:200]
                failures.append(f"{classname}#{method}: {message}")
                if len(failures) >= limit:
                    return failures
    return failures


def parse_junit_failures(working_dir: str, limit: int = 10) -> list[str]:
    """Names the specific test(s) that failed a checkpoint's full build.

    `gradle -q build` (quiet console mode) never prints individual failing
    test names or stack traces — only a summary count ("5 failed, 7
    skipped") — so the only place that detail exists is the JUnit XML
    reports the Test task writes regardless of console verbosity. Tries
    Gradle's report path first, then Maven Surefire's — both use the same
    TEST-<classname>.xml schema, just a different directory."""
    for rel in _JUNIT_REPORT_DIRS:
        reports_dir = os.path.join(working_dir, rel)
        if os.path.isdir(reports_dir):
            failures = _parse_junit_failures(reports_dir, limit)
            if failures:
                return failures
    return []

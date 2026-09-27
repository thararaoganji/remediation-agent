import subprocess

from core.tools import patch_tools


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


# --- apply_diff (real git repo) ---------------------------------------------

def test_apply_diff_applies_valid_unified_diff(git_repo):
    target = git_repo / "A.java"
    target.write_text("class A {\n  int x = 1;\n}\n")
    _git(["add", "A.java"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    diff = (
        "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,3 @@\n"
        " class A {\n-  int x = 1;\n+  int x = 2;\n }\n"
    )
    ok = patch_tools.apply_diff(diff, str(git_repo), "A.java")
    assert ok is True
    assert "int x = 2;" in target.read_text()


def test_apply_diff_returns_false_for_empty_diff(git_repo):
    (git_repo / "A.java").write_text("class A {}\n")
    assert patch_tools.apply_diff("", str(git_repo), "A.java") is False
    assert patch_tools.apply_diff("   \n", str(git_repo), "A.java") is False


def test_apply_diff_returns_false_for_malformed_diff(git_repo):
    (git_repo / "A.java").write_text("class A {}\n")
    _git(["add", "A.java"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    ok = patch_tools.apply_diff("not a real diff at all", str(git_repo), "A.java")
    assert ok is False
    assert (git_repo / "A.java").read_text() == "class A {}\n"  # untouched


# --- parse_junit_failures ----------------------------------------------------

_GRADLE_XML = """<?xml version="1.0"?>
<testsuite name="com.example.FooTest">
  <testcase name="worksFine" classname="com.example.FooTest"/>
  <testcase name="breaksBadly" classname="com.example.FooTest">
    <failure message="expected true but was false">stack trace here</failure>
  </testcase>
</testsuite>
"""


def test_parse_junit_failures_gradle_path(tmp_path):
    reports_dir = tmp_path / "build" / "test-results" / "test"
    reports_dir.mkdir(parents=True)
    (reports_dir / "TEST-com.example.FooTest.xml").write_text(_GRADLE_XML)

    failures = patch_tools.parse_junit_failures(str(tmp_path))
    assert len(failures) == 1
    assert "breaksBadly" in failures[0]
    assert "expected true but was false" in failures[0]
    assert "worksFine" not in failures[0]


def test_parse_junit_failures_maven_path_used_when_gradle_absent(tmp_path):
    reports_dir = tmp_path / "target" / "surefire-reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "TEST-com.example.BarTest.xml").write_text(_GRADLE_XML.replace("FooTest", "BarTest"))

    failures = patch_tools.parse_junit_failures(str(tmp_path))
    assert len(failures) == 1
    assert "BarTest" in failures[0]


def test_parse_junit_failures_no_reports_returns_empty(tmp_path):
    assert patch_tools.parse_junit_failures(str(tmp_path)) == []

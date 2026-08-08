import os
import subprocess

from sonar_autofix_agent.tools import patch_tools


def issue(rule_key="java:S1", key="k1", line=1, end_line=None, start_off=0, end_off=10):
    return {
        "issue_key": key, "rule_key": rule_key,
        "start_line": line, "end_line": end_line or line,
        "start_offset": start_off, "end_offset": end_off,
    }


# --- classify_and_prepare_batch / issues_for_prompt -------------------------

def test_classify_independent_issues_different_lines():
    issues = [issue(key="a", line=1), issue(key="b", line=5)]
    result = patch_tools.classify_and_prepare_batch(issues)
    assert {i["issue_key"] for i in result.independent} == {"a", "b"}
    assert result.nested_pairs == []
    assert result.colliding_flagged == []


def test_classify_nested_pair_same_line():
    outer = issue(key="outer", line=3, start_off=0, end_off=100)
    inner = issue(key="inner", line=3, start_off=10, end_off=20)
    result = patch_tools.classify_and_prepare_batch([outer, inner])
    assert result.independent == []
    assert len(result.nested_pairs) == 1
    assert result.colliding_flagged == []


def test_classify_colliding_pair_flagged_and_excluded():
    a = issue(key="a", line=3, start_off=0, end_off=15)
    b = issue(key="b", line=3, start_off=10, end_off=25)
    result = patch_tools.classify_and_prepare_batch([a, b])
    assert result.independent == []
    assert result.nested_pairs == []
    assert {i["issue_key"] for i in result.colliding_flagged} == {"a", "b"}

    prompt_issues = patch_tools.issues_for_prompt(result)
    assert prompt_issues == []  # colliding issues never reach the LLM prompt


def test_issues_for_prompt_bottom_to_top_order():
    issues = [issue(key="top", line=2), issue(key="bottom", line=10)]
    result = patch_tools.classify_and_prepare_batch(issues)
    ordered = patch_tools.issues_for_prompt(result)
    assert [i["issue_key"] for i in ordered] == ["bottom", "top"]


# --- apply_diff (real git repo) ---------------------------------------------

def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


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


# --- _is_meaningful_flagged_text --------------------------------------------

def test_is_meaningful_flagged_text_rejects_bare_brace():
    assert patch_tools._is_meaningful_flagged_text("}") is False
    assert patch_tools._is_meaningful_flagged_text("  }  ") is False
    assert patch_tools._is_meaningful_flagged_text("});") is False


def test_is_meaningful_flagged_text_accepts_real_code():
    assert patch_tools._is_meaningful_flagged_text('throw new RuntimeException("x");') is True
    assert patch_tools._is_meaningful_flagged_text("void findAll() {") is True


# --- verify_issue_patterns_resolved (count-based before/after) -------------

def _write(tmp_path, rel_path, content):
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def test_verify_resolved_when_flagged_occurrence_removed_but_unrelated_one_remains(tmp_path):
    """The exact false-positive scenario from this session: two S112
    issues genuinely fixed, one unrelated pre-existing occurrence of the
    same pattern elsewhere in the file must not poison the verdict."""
    before = (
        'void a() { throw new RuntimeException("x"); }\n'
        'void b() { doStuff().orElseThrow(() -> new RuntimeException("y")); }\n'
        'void c() { throw new RuntimeException("unrelated, never flagged"); }\n'
    )
    after = (
        'void a() { throw new IllegalStateException("x"); }\n'
        'void b() { doStuff().orElseThrow(() -> new IllegalStateException("y")); }\n'
        'void c() { throw new RuntimeException("unrelated, never flagged"); }\n'
    )
    _write(tmp_path, "A.java", after)
    issues = [issue("java:S112", "k1", 1), issue("java:S112", "k2", 2)]
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content=before,
    )
    assert result == {"k1": True, "k2": True}


def test_verify_unresolved_when_flagged_occurrence_still_present(tmp_path):
    before = 'void a() { throw new RuntimeException("x"); }\n'
    after = before  # not actually fixed
    _write(tmp_path, "A.java", after)
    issues = [issue("java:S112", "k1", 1)]
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content=before,
    )
    assert result == {"k1": False}


def test_verify_falls_back_to_after_only_check_without_original_content(tmp_path):
    _write(tmp_path, "A.java", 'var l = x.stream().collect(Collectors.toList());\n')
    issues = [issue("java:S6204", "k1", 1)]
    result = patch_tools.verify_issue_patterns_resolved("A.java", issues, str(tmp_path))
    assert result == {"k1": False}

    _write(tmp_path, "A.java", "var l = x.stream().toList();\n")
    result = patch_tools.verify_issue_patterns_resolved("A.java", issues, str(tmp_path))
    assert result == {"k1": True}


def test_verify_unresolved_when_count_math_masks_the_actual_flagged_line(tmp_path):
    """Regression: exact live bug (ExpenseService.java). Two S112 issues
    flagged, at lines 1 and 2. The LLM only fixes line 2, but also edits
    a THIRD, never-flagged occurrence of the same pattern elsewhere in
    the file -- the aggregate count still drops by 2, satisfying the
    before/after math, even though line 1's actual flagged violation is
    untouched. Verified live: reported as fully resolved and silently
    left open on the next Sonar scan."""
    before = (
        'void a() { throw new RuntimeException("still here"); }\n'
        'void b() { throw new RuntimeException("gets fixed"); }\n'
        'void c() { throw new RuntimeException("unflagged, but edited anyway"); }\n'
    )
    after = (
        'void a() { throw new RuntimeException("still here"); }\n'
        'void b() { throw new IllegalStateException("gets fixed"); }\n'
        'void c() { throw new IllegalStateException("unflagged, but edited anyway"); }\n'
    )
    _write(tmp_path, "A.java", after)
    issues = [issue("java:S112", "k1", 1), issue("java:S112", "k2", 2)]
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content=before,
    )
    assert result == {"k1": False, "k2": True}


def test_verify_unknown_rule_key_resolved_once_flagged_line_changes(tmp_path):
    _write(tmp_path, "A.java", "changed content\n")
    issues = [issue("java:S9999", "k1", 1)]
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content="whatever content\n",
    )
    assert result == {"k1": True}


def test_verify_unknown_rule_key_unresolved_when_flagged_line_untouched(tmp_path):
    """Regression: exact live bug (spring-petclinic). S4684 has no
    _VIOLATION_COUNT entry, so it used to default straight to
    resolved=True with zero verification. PetController.java's five
    S4684 (Security) issues were never touched across two separate fix
    attempts in the same run -- both reported "resolved", the file
    marked fixed, and Security stayed pinned at D with no record the
    issues were ever left open. The per-line check needs no rule-specific
    pattern: if the flagged line is untouched, it wasn't fixed."""
    _write(tmp_path, "A.java", "whatever content\n")
    issues = [issue("java:S4684", "k1", 1)]
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content="whatever content\n",
    )
    assert result == {"k1": False}


def test_verify_resolved_when_flagged_line_is_a_bare_closing_brace(tmp_path):
    """Regression: exact live bug (spring-petclinic). S2699's flagged
    range for a "test has no assertion" issue can be just the method's
    closing brace. A genuine fix (adding a real assertThat(...) call
    inside the method body) was reported unresolved anyway, because a
    bare '}' trivially still appears elsewhere in the file -- on every
    other method's closing line -- regardless of whether THIS method was
    actually fixed."""
    before = (
        "class T {\n"
        "  @Test\n"
        "  void findAll() {\n"
        "    vets.findAll();\n"
        "  }\n"
        "}\n"
    )
    after = (
        "class T {\n"
        "  @Test\n"
        "  void findAll() {\n"
        "    assertThat(vets.findAll()).isNotEmpty();\n"
        "  }\n"
        "}\n"
    )
    _write(tmp_path, "A.java", after)
    issues = [issue("java:S2699", "k1", 5, 5)]  # flagged range: just the closing "  }" line
    result = patch_tools.verify_issue_patterns_resolved(
        "A.java", issues, str(tmp_path), original_content=before,
    )
    assert result == {"k1": True}


def test_verify_unknown_rule_key_defaults_to_resolved_without_original_content(tmp_path):
    """Without original_content, there's nothing to diff the flagged line
    against -- falls back to the old no-verification default rather than
    guessing."""
    _write(tmp_path, "A.java", "whatever content\n")
    issues = [issue("java:S9999", "k1", 1)]
    result = patch_tools.verify_issue_patterns_resolved("A.java", issues, str(tmp_path))
    assert result == {"k1": True}

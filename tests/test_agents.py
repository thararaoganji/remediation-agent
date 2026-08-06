"""
agents.py's BaseAgent orchestration classes need a real ADK InvocationContext
to exercise directly (see README's "Running tests" section) -- out of scope
here. This covers the plain, context-free helper functions instead.
"""

from sonar_autofix_agent.agents import _looks_like_diff, _extract_code_block, _java_fqcn


# --- _looks_like_diff --------------------------------------------------

def test_looks_like_diff_detects_embedded_diff_git_header():
    """Regression: a full-file retry that degrades back into diff-shaped
    output ends with a literal 'diff --git a/...' block instead of real
    source -- observed live across 5 files in one run, all reverted only
    because the compile-check caught them after the fact. This guard
    catches it before ever writing to disk."""
    corrupted = (
        "package portal.expenses.util;\nclass A {}\n"
        "diff --git a/src/main/java/A.java b/src/main/java/A.java\n"
        "--- a/src/main/java/A.java\n"
        "+++ b/src/main/java/A.java\n"
        "@@ -12,13 +12,25 @@ public class A {\n"
    )
    assert _looks_like_diff(corrupted) is True


def test_looks_like_diff_detects_hunk_header_alone():
    text = "class A {}\n@@ -1,3 +1,3 @@ some context\n"
    assert _looks_like_diff(text) is True


def test_looks_like_diff_false_for_clean_java_source():
    clean = (
        "package portal.expenses.util;\n\n"
        "class A {\n"
        "  void f() {\n"
        "    System.out.println(\"ok\");\n"
        "  }\n"
        "}\n"
    )
    assert _looks_like_diff(clean) is False


def test_looks_like_diff_false_for_source_mentioning_at_symbols():
    # annotations and email-like strings shouldn't false-positive
    text = '@Service\nclass A {\n  String s = "user@@example.com";\n}\n'
    assert _looks_like_diff(text) is False


# --- _extract_code_block -------------------------------------------------

def test_extract_code_block_pulls_fenced_content():
    text = "Here's the fix:\n```java\nclass A {}\n```\nHope that helps."
    assert _extract_code_block(text) == "class A {}\n"


def test_extract_code_block_falls_back_to_raw_text_when_no_fence():
    text = "class A {}"
    assert _extract_code_block(text) == text


# --- _java_fqcn ------------------------------------------------------------

def test_java_fqcn_strips_source_root_prefix():
    assert _java_fqcn("src/test/java/portal/expenses/controller/AuthControllerTest.java") \
        == "portal.expenses.controller.AuthControllerTest"


def test_java_fqcn_handles_main_source_root():
    assert _java_fqcn("src/main/java/portal/expenses/service/ExpenseService.java") \
        == "portal.expenses.service.ExpenseService"

from techdebt_agent.prompts import JAVA_SPRING_ADDENDUM, build_fix_prompt, build_issue_block


def _issue(key="k1", rule_key="java:S106"):
    return {
        "rule_key": rule_key, "rule_name": "Rule Name", "category": "MAINTAINABILITY",
        "severity": "MAJOR", "start_line": 5, "end_line": 5,
        "message": "don't do that", "rule_description": "explanation here",
    }


def test_build_issue_block_includes_all_fields():
    block = build_issue_block([_issue()])
    assert "java:S106" in block
    assert "Rule Name" in block
    assert "5-5" in block
    assert "don't do that" in block
    assert "explanation here" in block


def test_build_issue_block_multiple_issues_all_present():
    block = build_issue_block([_issue("k1", "java:S106"), _issue("k2", "java:S112")])
    assert "java:S106" in block
    assert "java:S112" in block


def test_build_fix_prompt_includes_file_and_content_and_addendum():
    prompt = build_fix_prompt(
        file_path="src/main/java/A.java",
        language="java",
        file_content="class A {}\n",
        issues_bottom_to_top=[_issue()],
        language_addendum="LANGUAGE ADDENDUM MARKER",
    )
    assert "src/main/java/A.java" in prompt
    assert "class A {}" in prompt
    assert "LANGUAGE ADDENDUM MARKER" in prompt
    assert "unified diff" in prompt  # default output_format


def test_build_fix_prompt_custom_output_format_overrides_default():
    prompt = build_fix_prompt(
        file_path="A.java", language="java", file_content="x",
        issues_bottom_to_top=[_issue()], language_addendum="",
        output_format="CUSTOM FULL-FILE FORMAT MARKER",
    )
    assert "CUSTOM FULL-FILE FORMAT MARKER" in prompt


def test_java_spring_addendum_requires_null_safe_self_invocation_fallback():
    """Regression: exact live bug (be-exps-portal's ExpenseService.java).
    Two independent runs generated different S6809 self-injection fixes --
    one included `(self != null ? self : this)` and its checkpoint passed;
    the other called `self.createExpense(...)` directly and NPE'd under the
    class's Mockito-only unit tests (self is never populated without a real
    Spring context). The addendum must mandate the null-safe fallback, not
    just mention it as a nice-to-have, since the model doesn't reliably
    include it on its own."""
    assert "self != null ? self : this" in JAVA_SPRING_ADDENDUM
    assert "MANDATORY" in JAVA_SPRING_ADDENDUM

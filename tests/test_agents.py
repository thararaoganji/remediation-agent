"""
agents.py's BaseAgent orchestration classes need a real ADK InvocationContext
to exercise directly (see README's "Running tests" section) -- out of scope
here. This covers the plain, context-free helper functions instead.
"""

from google.adk.events import Event, EventActions
from google.genai import types

from sonar_autofix_agent import state_schema as sk
from sonar_autofix_agent.agents import (
    _looks_like_diff, _extract_code_block, _java_fqcn, _hide_text, _strip_escalate, _scanned_branch,
)


# --- _scanned_branch -----------------------------------------------------

def test_scanned_branch_falls_back_to_default_when_nothing_fixed():
    """Regression: a run that fixes zero files never fires a checkpoint,
    so its own agent branch never gets Sonar-analyzed under its own name
    -- querying it directly either crashed
    (get_maintainability_debt_ratio) or silently misreported "all A"
    (get_quality_ratings on an empty {}). branch=None (falling back to
    the project's default branch, still byte-identical to the new branch
    at that point) is the fix."""
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: []}
    assert _scanned_branch(s) is None


def test_scanned_branch_uses_own_branch_once_something_was_fixed():
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: ["A.java"]}
    assert _scanned_branch(s) == "my-project_agent_20260101_000000"


# --- _strip_escalate -----------------------------------------------------

def test_strip_escalate_clears_the_flag():
    """Regression: ADK's LoopAgent re-yields sub-agent events unmodified
    and checks event.actions.escalate at every nesting level -- so
    per_file_loop's own queue-empty escalate=True also terminated
    whatever LoopAgent embedded it (outer_loop, maintainability_expansion_loop)
    the moment it bubbled through. Confirmed live: outer_loop had never
    run a second iteration in this project's history (OUTER_ITERATION
    stayed 0 in every final report) even when files ended up flagged and
    were meant to get a re-fetch-and-retry attempt."""
    event = Event(author="file_fixer_step", actions=EventActions(escalate=True))
    stripped = _strip_escalate(event)
    assert stripped.actions.escalate is False


def test_strip_escalate_passes_through_non_escalating_events_unchanged():
    event = Event(author="file_fixer_step", actions=EventActions(escalate=False))
    assert _strip_escalate(event) is event


def test_strip_escalate_passes_through_default_actions_unchanged():
    event = Event(author="file_fixer_step")
    assert _strip_escalate(event) is event


# --- _hide_text --------------------------------------------------------

def test_hide_text_strips_content_but_preserves_state_delta():
    """Regression: dropping a text-bearing event outright (via `continue`,
    the pattern this replaced) also drops its state_delta, since ADK only
    applies state_delta when an event reaches the top-level Runner via
    session_service.append_event -- confirmed live, this silently broke
    fix_llm_agent's output_key write (KeyError: 'temp:proposed_diff')
    and had been breaking _retry_full_file's read of the regenerated
    file the same way for the whole session before this fix."""
    event = Event(
        author="fix_llm_agent",
        content=types.Content(role="model", parts=[types.Part(text="explanation + diff")]),
        actions=EventActions(state_delta={"temp:proposed_diff": "the actual diff"}),
    )
    hidden = _hide_text(event)
    assert hidden.content is None
    assert hidden.actions.state_delta == {"temp:proposed_diff": "the actual diff"}


def test_hide_text_passes_through_non_text_events_unchanged():
    event = Event(
        author="fix_llm_agent",
        content=types.Content(role="user", parts=[types.Part(function_response={"name": "x", "response": {}})]),
        actions=EventActions(state_delta={"source": "some/repo"}),
    )
    result = _hide_text(event)
    assert result is event  # untouched, not even copied


def test_hide_text_passes_through_content_none_unchanged():
    event = Event(author="x", actions=EventActions())
    assert _hide_text(event) is event


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

"""
The agents package's BaseAgent orchestration classes need a real ADK InvocationContext
to exercise directly (see README's "Running tests" section) -- out of scope
here. This covers the plain, context-free helper functions instead.
"""

from google.adk.events import Event, EventActions
from google.genai import types

from sonar_autofix_agent import state_schema as sk
from sonar_autofix_agent.agents import (
    _looks_like_diff, _extract_code_block, _java_fqcn, _hide_text, _strip_escalate, _scanned_branch,
    _no_safe_fix_reason, _format_summary,
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
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: [], "ce_edition": False}
    assert _scanned_branch(s) is None


def test_scanned_branch_uses_own_branch_once_something_was_fixed():
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: ["A.java"], "ce_edition": False}
    assert _scanned_branch(s) == "my-project_agent_20260101_000000"


def test_scanned_branch_always_none_under_ce_edition_even_with_files_fixed():
    """Regression: Community Edition has no branch-aware analysis at all
    (Developer+ only) -- trigger_sonar_analysis() never passes
    -Dsonar.branch.name under ce_edition, so the agent's own branch is
    never created as a server-side entity in Sonar's data model, no
    matter how many files got committed. Confirmed live: a 5-file WebGoat
    run under CE_EDITION still 404'd on /api/measures/component for the
    agent's own branch name -- FILES_COMPLETED being non-empty is not a
    valid "this branch was scanned" signal under CE the way it is for
    Developer+."""
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: ["A.java"], "ce_edition": True}
    assert _scanned_branch(s) is None


def test_scanned_branch_defaults_to_ce_edition_when_key_missing():
    s = {sk.BRANCH_NAME: "my-project_agent_20260101_000000", sk.FILES_COMPLETED: ["A.java"]}
    assert _scanned_branch(s) is None


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


# --- _format_summary ------------------------------------------------------

def _report(**overrides) -> dict:
    base = {
        "branch_name": "proj_agent_20260101_000000",
        "issues_fixed": [],
        "files_completed": [],
        "files_flagged_for_manual_review": [],
        "issues_no_safe_fix": [],
        "outer_iterations": 1,
        "hit_max_iterations": False,
        "checkpoints": [],
        "final_ratings": {"security_rating": "1.0", "reliability_rating": "1.0", "sqale_rating": "1.0"},
        "all_categories_a": True,
        "wont_fix_review_queue": [],
        "push_result": "not attempted",
        "duration_seconds": 12.0,
        "tokens_consumed": {"prompt_tokens": 100, "candidates_tokens": 50, "total_tokens": 150},
        "note_if_not_a": None,
    }
    base.update(overrides)
    return base


def test_format_summary_flagged_entries_show_no_safe_fix_count():
    """Regression: issues_no_safe_fix was collected in state but never
    surfaced anywhere in the report -- a reviewer had no way to tell "the
    model made a judgment call here" apart from "the fix attempt failed
    for some other reason" without parsing free-text reasons."""
    report = _report(
        files_flagged_for_manual_review=[
            {"file": "A.java", "reason": "cannot be safely fixed without breaking templates"},
            {"file": "B.java", "reason": "still failed to compile"},
        ],
        issues_no_safe_fix=["k1"],
    )
    summary = _format_summary(report)
    assert "Flagged for manual review (2) — 1 declined as unsafe to auto-fix:" in summary


def test_format_summary_flagged_entries_omit_count_when_zero_no_safe_fix():
    report = _report(
        files_flagged_for_manual_review=[{"file": "B.java", "reason": "still failed to compile"}],
        issues_no_safe_fix=[],
    )
    summary = _format_summary(report)
    assert "Flagged for manual review (1):" in summary
    assert "declined as unsafe" not in summary


def test_format_summary_no_flagged_section_when_nothing_flagged():
    summary = _format_summary(_report())
    assert "Flagged for manual review" not in summary


# --- _no_safe_fix_reason -------------------------------------------------

def test_no_safe_fix_reason_detects_marker_and_extracts_reason():
    """Regression: the fix prompt tells the model to respond with
    'NO_SAFE_FIX: <reason>' instead of guessing, but nothing ever detected
    it -- the refusal text fell through as if it were valid diff/full-file
    content and got written to disk verbatim. Confirmed live: exactly this
    text corrupted VisitController.java into invalid Java."""
    text = (
        "NO_SAFE_FIX: S4684 cannot be safely fixed without refactoring the "
        "controller's public API to use DTOs, which breaks existing Spring "
        "MVC integration tests."
    )
    reason = _no_safe_fix_reason(text)
    assert reason is not None
    assert reason.startswith("S4684 cannot be safely fixed")


def test_no_safe_fix_reason_returns_none_for_normal_diff():
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    assert _no_safe_fix_reason(diff) is None


def test_no_safe_fix_reason_returns_none_for_clean_full_file_content():
    source = "package a.b;\n\nclass A {\n  void f() {}\n}\n"
    assert _no_safe_fix_reason(source) is None


def test_no_safe_fix_reason_falls_back_when_reason_text_empty():
    assert _no_safe_fix_reason("NO_SAFE_FIX:") == "no reason given"


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

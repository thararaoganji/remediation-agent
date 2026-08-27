"""Tests for coverage_agent and duplicate_agent's real per-file
loop architecture (enhance_coverage.py / fix_duplicate.py), which reuses
core's tool-agnostic fix-loop engine -- see those modules' docstrings for
what's reused as-is versus what's domain-specific.

This wholesale-replaces the previous version of this file, which tested the
placeholder implementation that predated the real loop: no LLM call,
`time.sleep(2.0)` standing in for real work, only ever the single most-
uncovered/most-duplicated file per run, and report steps with hardcoded
fake numbers that were never actually measured.

Follows test_orchestration.py's established approach for this codebase: a
real ADK Runner + InMemorySessionService drives each BaseAgent step (or a
small hand-assembled pipeline of them), with the LLM call and adapter/build
calls stubbed out -- no real network, LLM, or build calls. See that file's
module docstring for the reasoning behind _Seed/_Echo (temp:-prefixed keys
and same-invocation state visibility)."""

import asyncio
import subprocess
import time
import uuid

from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from core import state_schema as sk
from core.adapters.base import BuildResult
from core.agents.fix_loop import PerFileLoopStep
from coverage_agent import enhance_coverage
from duplicate_agent import fix_duplicate

APP_NAME = "test_multi_agent"


class _Seed(BaseAgent):
    name: str = "seed"
    data: dict

    async def _run_async_impl(self, ctx):
        yield Event(author=self.name, actions=EventActions(state_delta=self.data))


class _Echo(BaseAgent):
    name: str = "echo"

    async def _run_async_impl(self, ctx):
        renamed = {k.removeprefix("temp:"): v for k, v in ctx.session.state.items()}
        yield Event(author=self.name, actions=EventActions(state_delta=renamed))


def _run_agent(agent: BaseAgent, initial_state: dict) -> tuple[list[Event], dict]:
    seeded = SequentialAgent(name="seeded_under_test", sub_agents=[_Seed(data=initial_state), agent, _Echo()])

    async def _run():
        session_service = InMemorySessionService()
        user_id, session_id = "test_user", str(uuid.uuid4())
        await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        runner = Runner(agent=seeded, app_name=APP_NAME, session_service=session_service)
        trigger = types.Content(role="user", parts=[types.Part(text="start")])
        events = []
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=trigger):
            events.append(event)
        final_session = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        return events, final_session.state
    return asyncio.run(_run())


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _stub_fix_llm_agent(text: str):
    class _Stub:
        async def run_async(self, ctx):
            ctx.session.state[sk.PROPOSED_DIFF] = text
            yield Event(author="fix_llm_agent", actions=EventActions(state_delta={sk.PROPOSED_DIFF: text}))
    return _Stub()


def _patch_llm_agent_class(monkeypatch, text: str) -> None:
    """For the full-loop tests below, which build a real per-file loop via
    _build_(coverage|duplicate)_per_file_loop() -- that constructs a genuine
    FixLlmGateStep(llm_agent=...), and FixLlmGateStep.llm_agent is a
    pydantic-typed LlmAgent field, so a plain stub object (as _stub_fix_
    llm_agent returns, fine for a bare local variable elsewhere in this
    file) fails validation there. Patching LlmAgent.run_async at the CLASS
    level instead sidesteps that: _build_fix_llm_agent() constructs a real,
    valid LlmAgent (harmless -- pydantic field assignment only, no network
    call happens at construction time), and every instance's .run_async
    resolves to this stub instead of actually calling the model."""
    async def _stub_run_async(self, ctx):
        ctx.session.state[sk.PROPOSED_DIFF] = text
        yield Event(author=self.name, actions=EventActions(state_delta={sk.PROPOSED_DIFF: text}))
    monkeypatch.setattr(LlmAgent, "run_async", _stub_run_async)


def _fake_adapter(*, compile_passed=True, tests_passed=True, errors="boom"):
    class _FakeAdapter:
        def quick_compile_check(self, working_dir, scope):
            return BuildResult(passed=compile_passed, errors="" if compile_passed else errors)

        def run_specific_tests(self, working_dir, test_classes):
            return BuildResult(passed=tests_passed, errors="" if tests_passed else errors)
    return _FakeAdapter()


class _NoOpCheckpointGate(BaseAgent):
    """Stands in for the real CheckpointGate in the end-to-end loop tests
    below -- checkpoint bisection/re-scan behavior is already covered by
    test_orchestration.py's RunFullVerifyStep tests, and is entirely
    domain-agnostic (reused as-is by both new agents), so re-exercising it
    here would just be redundant, not new coverage."""
    name: str = "checkpoint_gate"

    async def _run_async_impl(self, ctx):
        if False:  # pragma: no cover -- keeps this an async generator
            yield


# --- CoverageFetchStep -------------------------------------------------

def test_coverage_fetch_step_excludes_completed_flagged_and_reverted_files(monkeypatch):
    files = [
        {"file": "A.java", "coverage": 40.0, "uncovered_lines": 5, "uncovered_conditions": 1},
        {"file": "B.java", "coverage": 20.0, "uncovered_lines": 10, "uncovered_conditions": 2},
        {"file": "C.java", "coverage": 10.0, "uncovered_lines": 20, "uncovered_conditions": 3},
        {"file": "D.java", "coverage": 5.0, "uncovered_lines": 30, "uncovered_conditions": 4},
    ]
    monkeypatch.setattr(enhance_coverage, "fetch_uncovered_files", lambda *a, **kw: files)

    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.FILES_COMPLETED: ["A.java"],
        sk.FILES_FLAGGED: [{"file": "B.java", "reason": "declined"}],
        sk.FILES_REVERTED_AT_CHECKPOINT: ["C.java"],
    }
    _, final_state = _run_agent(enhance_coverage.CoverageFetchStep(), initial_state)
    assert [f["file"] for f in final_state[sk.ORDERED_FILES_REMAINING]] == ["D.java"]


# --- CoverageApplyAndVerifyStep -----------------------------------------

def test_coverage_apply_and_verify_writes_new_test_file_and_commits(git_repo, monkeypatch):
    (git_repo / "src" / "main" / "java" / "pkg").mkdir(parents=True)
    (git_repo / "src" / "main" / "java" / "pkg" / "Foo.java").write_text("package pkg;\nclass Foo {}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}
    new_test_content = "package pkg;\nimport org.junit.jupiter.api.Test;\nclass FooTest {\n  @Test void t() {}\n}\n"
    monkeypatch.setattr(enhance_coverage, "get_adapter", lambda *a, **kw: _fake_adapter())

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: f"```java\n{new_test_content}```",
        "temp:test_file_pre_existed": False,
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert final_state[sk.FILES_COMPLETED] == [entry["file"]]
    assert final_state[sk.ORDERED_FILES_REMAINING] == []
    test_path = git_repo / "src/test/java/pkg/FooTest.java"
    assert test_path.read_text() == new_test_content.strip()  # _extract_code_block(...).strip()
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(git_repo), capture_output=True, text=True).stdout
    assert "coverage" in log


def test_coverage_apply_and_verify_reverts_brand_new_file_on_test_failure(git_repo, monkeypatch):
    (git_repo / "src" / "main" / "java" / "pkg").mkdir(parents=True)
    (git_repo / "src" / "main" / "java" / "pkg" / "Foo.java").write_text("package pkg;\nclass Foo {}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}
    monkeypatch.setattr(enhance_coverage, "get_adapter", lambda *a, **kw: _fake_adapter(tests_passed=False))
    # The failure triggers a retry -- stub its LLM call too (with content
    # that _fake_adapter(tests_passed=False) will also reject, so the
    # retry-then-still-fails path is what's under test here).
    _patch_llm_agent_class(monkeypatch, "```java\npackage pkg;\nclass FooTest { void retried() {} }\n```")

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "```java\npackage pkg;\nclass FooTest { void t() { throw new RuntimeException(); } }\n```",
        "temp:test_file_pre_existed": False,
        sk.CURRENT_FILE_CONTENT: "",
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert not (git_repo / "src/test/java/pkg/FooTest.java").exists()  # brand-new file, deleted not reverted
    assert final_state[sk.FILES_COMPLETED] == []
    assert final_state[sk.FILES_FLAGGED][0]["file"] == entry["file"]
    assert final_state[sk.ORDERED_FILES_REMAINING] == []


def test_coverage_apply_and_verify_restores_pre_existing_file_on_failure(git_repo, monkeypatch):
    """Same failure path, but the test file already existed -- must be
    restored via git (index/HEAD), not deleted, or the pre-existing tests
    it held would be lost."""
    (git_repo / "src" / "main" / "java" / "pkg").mkdir(parents=True)
    (git_repo / "src" / "main" / "java" / "pkg" / "Foo.java").write_text("package pkg;\nclass Foo {}\n")
    (git_repo / "src" / "test" / "java" / "pkg").mkdir(parents=True)
    original_test = "package pkg;\nclass FooTest {\n  void existing() {}\n}\n"
    (git_repo / "src" / "test" / "java" / "pkg" / "FooTest.java").write_text(original_test)
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}
    monkeypatch.setattr(enhance_coverage, "get_adapter", lambda *a, **kw: _fake_adapter(tests_passed=False))
    _patch_llm_agent_class(monkeypatch, "```java\npackage pkg;\nclass FooTest { void retried() {} }\n```")

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "```java\npackage pkg;\nclass FooTest { void broken() { throw new RuntimeException(); } }\n```",
        "temp:test_file_pre_existed": True,
        sk.CURRENT_FILE_CONTENT: original_test,
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert (git_repo / "src/test/java/pkg/FooTest.java").read_text() == original_test
    assert final_state[sk.FILES_FLAGGED][0]["file"] == entry["file"]


def test_coverage_apply_and_verify_commits_after_a_successful_retry(git_repo, monkeypatch):
    """The actual bug this was added for: every one of a real run's 9
    failures was a first-attempt compile error the model could very
    plausibly have self-corrected given the compiler's own message (a
    hallucinated setter/enum constant/constructor arity on some OTHER
    class it was never shown) -- but ApplyAndVerifyStep had no retry at
    all, so a fixable first miss was always a final one. This is the
    retry's happy path: first attempt fails, retry succeeds, the file
    ends up committed, not discarded."""
    (git_repo / "src" / "main" / "java" / "pkg").mkdir(parents=True)
    (git_repo / "src" / "main" / "java" / "pkg" / "Foo.java").write_text("package pkg;\nclass Foo {}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}

    calls = {"n": 0}

    class _FailsOnceAdapter:
        def run_specific_tests(self, working_dir, test_classes):
            calls["n"] += 1
            return BuildResult(passed=calls["n"] > 1, errors="" if calls["n"] > 1 else "cannot find symbol: setBogus")

    monkeypatch.setattr(enhance_coverage, "get_adapter", lambda *a, **kw: _FailsOnceAdapter())
    fixed_content = "package pkg;\nclass FooTest { void t() {} }\n"
    _patch_llm_agent_class(monkeypatch, f"```java\n{fixed_content}```")

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "```java\npackage pkg;\nclass FooTest { void t() { bogus.setBogus(); } }\n```",
        "temp:test_file_pre_existed": False,
        sk.CURRENT_FILE_CONTENT: "",
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert calls["n"] == 2  # first attempt, then one retry -- not more
    assert (git_repo / "src/test/java/pkg/FooTest.java").read_text() == fixed_content.strip()
    assert final_state[sk.FILES_COMPLETED] == [entry["file"]]
    assert final_state[sk.FILES_FLAGGED] == []


def test_coverage_apply_and_verify_declines_via_no_safe_fix(git_repo):
    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}
    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "NO_SAFE_FIX: needs a real database connection to test meaningfully",
        "temp:test_file_pre_existed": False,
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert final_state[sk.FILES_FLAGGED][0]["reason"] == "needs a real database connection to test meaningfully"
    assert not (git_repo / "src/test/java/pkg/FooTest.java").exists()


def test_coverage_llm_call_error_flags_without_writing_anything(git_repo):
    entry = {"file": "src/main/java/pkg/Foo.java", "coverage": 50.0, "uncovered_lines": 3, "uncovered_conditions": 1}
    group = {"file": entry["file"], "test_file": "src/test/java/pkg/FooTest.java", "entry": entry}
    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        "temp:llm_call_error": "RECITATION: no further detail from the model API",
        "temp:test_file_pre_existed": False,
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(enhance_coverage.CoverageApplyAndVerifyStep(), initial_state)

    assert "RECITATION" in final_state[sk.FILES_FLAGGED][0]["reason"]
    assert not (git_repo / "src/test/java/pkg/FooTest.java").exists()


# --- coverage: full per-file loop, end to end ---------------------------

def test_coverage_full_per_file_loop_end_to_end(git_repo, monkeypatch):
    (git_repo / "src" / "main" / "java" / "pkg").mkdir(parents=True)
    (git_repo / "src" / "main" / "java" / "pkg" / "Foo.java").write_text("package pkg;\nclass Foo {}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    new_test_content = "package pkg;\nimport org.junit.jupiter.api.Test;\nclass FooTest {\n  @Test void t() {}\n}\n"
    monkeypatch.setattr(enhance_coverage, "fetch_uncovered_files", lambda *a, **kw: [
        {"file": "src/main/java/pkg/Foo.java", "coverage": 40.0, "uncovered_lines": 3, "uncovered_conditions": 1},
    ])
    _patch_llm_agent_class(monkeypatch, f"```java\n{new_test_content}```")
    monkeypatch.setattr(enhance_coverage, "get_adapter", lambda *a, **kw: _fake_adapter())
    monkeypatch.setattr(enhance_coverage, "build_checkpoint_gate", _NoOpCheckpointGate)

    loop = enhance_coverage._build_coverage_per_file_loop()
    pipeline = SequentialAgent(
        name="test_coverage_pipeline",
        sub_agents=[enhance_coverage.CoverageFetchStep(), PerFileLoopStep(loop=loop)],
    )
    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.WORKING_DIR: str(git_repo), sk.LANGUAGE: "java-maven",
        sk.FILES_COMPLETED: [], sk.FILES_FLAGGED: [], sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.ISSUES_FIXED: [], sk.FILES_SINCE_CHECKPOINT: 0, sk.ORDERED_FILES_REMAINING: [],
    }
    _, final_state = _run_agent(pipeline, initial_state)

    assert final_state[sk.FILES_COMPLETED] == ["src/main/java/pkg/Foo.java"]
    assert final_state[sk.ISSUES_FIXED] == ["coverage:src/main/java/pkg/Foo.java"]
    assert (git_repo / "src/test/java/pkg/FooTest.java").read_text() == new_test_content.strip()


# --- CoverageBaselineStep -------------------------------------------------

def test_coverage_baseline_step_captures_coverage_before(monkeypatch):
    monkeypatch.setattr(enhance_coverage, "get_metric_value", lambda *a, **kw: "42.5")
    initial_state = {sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t"}
    _, final_state = _run_agent(enhance_coverage.CoverageBaselineStep(), initial_state)
    assert final_state["coverage_before"] == 42.5


def test_coverage_baseline_step_handles_no_prior_analysis(monkeypatch):
    monkeypatch.setattr(enhance_coverage, "get_metric_value", lambda *a, **kw: None)
    initial_state = {sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t"}
    _, final_state = _run_agent(enhance_coverage.CoverageBaselineStep(), initial_state)
    assert final_state["coverage_before"] is None


# --- CoverageQualityGateStep -----------------------------------------------

def test_coverage_quality_gate_escalates_when_nothing_completed():
    events, _ = _run_agent(enhance_coverage.CoverageQualityGateStep(), {sk.FILES_COMPLETED: []})
    assert any(e.actions and e.actions.escalate for e in events)


def test_coverage_quality_gate_escalates_when_rating_already_a(monkeypatch):
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "1.0"})
    monkeypatch.setattr(enhance_coverage, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.FILES_COMPLETED: ["FooTest.java"],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    events, _ = _run_agent(enhance_coverage.CoverageQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)


def test_coverage_quality_gate_escalates_after_iteration_cap(monkeypatch):
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(enhance_coverage, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.FILES_COMPLETED: ["FooTest.java"],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        "temp:coverage_quality_iteration": 3,  # about to become 4, over the cap of 3
    }
    events, _ = _run_agent(enhance_coverage.CoverageQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)


def test_coverage_quality_gate_queues_new_smells_scoped_to_own_files(monkeypatch):
    """Regression guard for the actual feature request: only re-fix
    MAINTAINABILITY code smells Sonar found in files THIS coverage run
    wrote -- a different category, or a file this run never touched, is
    out of scope (pre-existing production-code debt belongs to
    techdebt_agent, not this agent silently expanding into it)."""
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(enhance_coverage, "_scanned_branch", lambda s: "my-branch")

    issues = [
        {  # in scope: MAINTAINABILITY, on a file this run completed
            "category": "MAINTAINABILITY", "severity": "MINOR",
            "component_path": "src/test/java/pkg/FooTest.java", "issue_key": "k1", "rule_key": "java:S1192",
            "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
        },
        {  # out of scope: different category
            "category": "SECURITY", "severity": "HIGH",
            "component_path": "src/test/java/pkg/FooTest.java", "issue_key": "k2", "rule_key": "java:S1",
            "rule_name": "sec", "start_line": 1, "end_line": 1, "message": "m",
        },
        {  # out of scope: not a file this run touched
            "category": "MAINTAINABILITY", "severity": "MINOR",
            "component_path": "src/main/java/pkg/Other.java", "issue_key": "k3", "rule_key": "java:S1192",
            "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
        },
    ]
    monkeypatch.setattr(enhance_coverage.sonar_tools, "fetch_issues_and_hotspots", lambda *a, **kw: issues)
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_rule_description", lambda *a, **kw: "desc")

    initial_state = {
        sk.FILES_COMPLETED: ["src/test/java/pkg/FooTest.java"],
        sk.FILES_FLAGGED: [], sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    _, final_state = _run_agent(enhance_coverage.CoverageQualityGateStep(), initial_state)

    queue = final_state[sk.ORDERED_FILES_REMAINING]
    assert len(queue) == 1
    assert queue[0]["file"] == "src/test/java/pkg/FooTest.java"
    assert [i["issue_key"] for i in queue[0]["issues"]] == ["k1"]
    assert queue[0]["issues"][0]["rule_description"] == "desc"


def test_coverage_quality_gate_excludes_already_flagged_or_reverted_files(monkeypatch):
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(enhance_coverage, "_scanned_branch", lambda s: "my-branch")

    issues = [{
        "category": "MAINTAINABILITY", "severity": "MINOR",
        "component_path": "src/test/java/pkg/FooTest.java", "issue_key": "k1", "rule_key": "java:S1192",
        "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
    }]
    monkeypatch.setattr(enhance_coverage.sonar_tools, "fetch_issues_and_hotspots", lambda *a, **kw: issues)
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_rule_description", lambda *a, **kw: "desc")

    initial_state = {
        sk.FILES_COMPLETED: ["src/test/java/pkg/FooTest.java"],
        sk.FILES_FLAGGED: [{"file": "src/test/java/pkg/FooTest.java", "reason": "already flagged earlier"}],
        sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    events, _ = _run_agent(enhance_coverage.CoverageQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)  # nothing left to queue


# --- CoverageReportStep: before/after delta --------------------------------

def test_coverage_report_shows_delta_and_maintainability_rating(monkeypatch):
    monkeypatch.setattr(enhance_coverage, "get_metric_value", lambda *a, **kw: "55.0")
    monkeypatch.setattr(enhance_coverage.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "1.0"})
    monkeypatch.setattr(enhance_coverage, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.BRANCH_NAME: "my-branch",
        sk.FILES_COMPLETED: ["FooTest.java"], sk.ISSUES_FIXED: ["coverage:Foo.java"],
        sk.FILES_FLAGGED: [], sk.RUN_START_TIME: time.time(),
        sk.TOKEN_USAGE: {"prompt_tokens": 1, "candidates_tokens": 1, "total_tokens": 2},
        "temp:coverage_before": 40.0,
    }
    events, _ = _run_agent(enhance_coverage.CoverageReportStep(), initial_state)
    text = next(e.content.parts[0].text for e in events if e.author == "coverage_report_step")
    assert "40.0% → 55.0% (+15.0 pts)" in text
    assert "Maintainability rating on this branch: A" in text


# --- DuplicateFetchStep --------------------------------------------------

def test_duplicate_fetch_step_excludes_completed_flagged_and_reverted_files(monkeypatch):
    files = [
        {"file": "A.java", "duplicated_lines_density": 40.0, "duplicated_blocks": 3},
        {"file": "B.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 2},
        {"file": "C.java", "duplicated_lines_density": 20.0, "duplicated_blocks": 1},
        {"file": "D.java", "duplicated_lines_density": 10.0, "duplicated_blocks": 1},
    ]
    monkeypatch.setattr(fix_duplicate, "fetch_duplicated_files", lambda *a, **kw: files)

    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.FILES_COMPLETED: ["A.java"],
        sk.FILES_FLAGGED: [{"file": "B.java", "reason": "declined"}],
        sk.FILES_REVERTED_AT_CHECKPOINT: ["C.java"],
    }
    _, final_state = _run_agent(fix_duplicate.DuplicateFetchStep(), initial_state)
    assert [f["file"] for f in final_state[sk.ORDERED_FILES_REMAINING]] == ["D.java"]


# --- _project_uses_lombok -------------------------------------------------

def test_project_uses_lombok_true_for_maven_dependency(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
        "</dependency></dependencies></project>"
    )
    assert fix_duplicate._project_uses_lombok(str(tmp_path)) is True


def test_project_uses_lombok_true_for_gradle_dependency(tmp_path):
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n  compileOnly 'org.projectlombok:lombok:1.18.30'\n}\n"
    )
    assert fix_duplicate._project_uses_lombok(str(tmp_path)) is True


def test_project_uses_lombok_false_when_absent(tmp_path):
    (tmp_path / "pom.xml").write_text("<project><dependencies></dependencies></project>")
    assert fix_duplicate._project_uses_lombok(str(tmp_path)) is False


def test_project_uses_lombok_false_when_no_build_file(tmp_path):
    assert fix_duplicate._project_uses_lombok(str(tmp_path)) is False


# --- DuplicateFileFixerStep: threads lombok_available into the prompt -----

def test_duplicate_file_fixer_step_prompt_offers_lombok_when_available(tmp_path):
    """Regression: exact live bug -- cross-file POJO/DTO/JPA-entity
    boilerplate duplication (fields + getters/setters, structurally
    similar to another class, not repeated within this one file) has
    nothing for the same-file helper-extraction rule to grab onto, so
    every such file got declined outright ("cannot be safely collapsed...
    without modifying other files"), even on a project that already had
    Lombok as a dependency and could have just adopted @Getter/@Setter."""
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
        "</dependency></dependencies></project>"
    )
    src = tmp_path / "src/main/java/pkg"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text("class Foo {\n  private int x;\n}\n")

    entry = {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 1}
    initial_state = {
        sk.WORKING_DIR: str(tmp_path),
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_FLAGGED: [],
    }
    final_state = _run_agent(fix_duplicate.DuplicateFileFixerStep(), initial_state)[1]
    assert "already depends on Lombok" in final_state["fix_prompt"]
    assert "@Getter" in final_state["fix_prompt"]


def test_duplicate_file_fixer_step_prompt_declines_lombok_when_unavailable(tmp_path):
    src = tmp_path / "src/main/java/pkg"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text("class Foo {\n  private int x;\n}\n")

    entry = {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 1}
    initial_state = {
        sk.WORKING_DIR: str(tmp_path),
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_FLAGGED: [],
    }
    final_state = _run_agent(fix_duplicate.DuplicateFileFixerStep(), initial_state)[1]
    assert "does NOT currently depend on Lombok" in final_state["fix_prompt"]


# --- DuplicateApplyAndVerifyStep -----------------------------------------

def test_duplicate_apply_and_verify_applies_diff_and_commits(git_repo, monkeypatch):
    src = git_repo / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text("class Foo {\n  int x = 1;\n}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    diff = (
        "--- a/src/main/java/pkg/Foo.java\n+++ b/src/main/java/pkg/Foo.java\n"
        "@@ -1,3 +1,3 @@\n class Foo {\n-  int x = 1;\n+  int x = helper();\n }\n"
    )
    entry = {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 2}
    group = {"file": entry["file"], "entry": entry}
    monkeypatch.setattr(fix_duplicate, "get_adapter", lambda *a, **kw: _fake_adapter())

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.CURRENT_FILE_CONTENT: "class Foo {\n  int x = 1;\n}\n",
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: diff,
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(fix_duplicate.DuplicateApplyAndVerifyStep(), initial_state)

    assert final_state[sk.FILES_COMPLETED] == [entry["file"]]
    assert "helper()" in (git_repo / "src/main/java/pkg/Foo.java").read_text()


def test_duplicate_apply_and_verify_retries_full_file_when_diff_fails_to_apply(git_repo, monkeypatch):
    src = git_repo / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    original = "class Foo {\n  int x = 1;\n}\n"
    (src / "Foo.java").write_text(original)
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    entry = {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 2}
    group = {"file": entry["file"], "entry": entry}
    full_file = "class Foo {\n  int x = helper();\n  private int helper() { return 1; }\n}\n"

    monkeypatch.setattr(fix_duplicate, "get_adapter", lambda *a, **kw: _fake_adapter())
    # The retry inside DuplicateApplyAndVerifyStep._retry_full_file makes
    # its own fresh _build_fix_llm_agent() call -- this stub is what it
    # gets back, independent of the (garbage) initial PROPOSED_DIFF below.
    monkeypatch.setattr(fix_duplicate, "_build_fix_llm_agent", lambda: _stub_fix_llm_agent(f"```java\n{full_file}```"))

    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.CURRENT_FILE_CONTENT: original,
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "this is not a valid diff at all",
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(fix_duplicate.DuplicateApplyAndVerifyStep(), initial_state)

    assert final_state[sk.FILES_COMPLETED] == [entry["file"]]
    assert (git_repo / "src/main/java/pkg/Foo.java").read_text() == full_file.strip()  # _extract_code_block(...).strip()


def test_duplicate_apply_and_verify_declines_via_no_safe_fix(git_repo):
    entry = {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 2}
    group = {"file": entry["file"], "entry": entry}
    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.CURRENT_FILE_CONTENT: "class Foo {}\n",
        sk.WORKING_DIR: str(git_repo),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "NO_SAFE_FIX: the two blocks differ in a way that can't be safely unified",
        sk.FILES_FLAGGED: [],
        sk.ORDERED_FILES_REMAINING: [entry],
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    _, final_state = _run_agent(fix_duplicate.DuplicateApplyAndVerifyStep(), initial_state)

    assert "can't be safely unified" in final_state[sk.FILES_FLAGGED][0]["reason"]
    assert final_state[sk.FILES_COMPLETED] == []


# --- duplication: full per-file loop, end to end -------------------------

def test_duplicate_full_per_file_loop_end_to_end(git_repo, monkeypatch):
    src = git_repo / "src" / "main" / "java" / "pkg"
    src.mkdir(parents=True)
    (src / "Foo.java").write_text("class Foo {\n  int x = 1;\n}\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    diff = (
        "--- a/src/main/java/pkg/Foo.java\n+++ b/src/main/java/pkg/Foo.java\n"
        "@@ -1,3 +1,3 @@\n class Foo {\n-  int x = 1;\n+  int x = helper();\n }\n"
    )
    monkeypatch.setattr(fix_duplicate, "fetch_duplicated_files", lambda *a, **kw: [
        {"file": "src/main/java/pkg/Foo.java", "duplicated_lines_density": 30.0, "duplicated_blocks": 2},
    ])
    _patch_llm_agent_class(monkeypatch, diff)
    monkeypatch.setattr(fix_duplicate, "get_adapter", lambda *a, **kw: _fake_adapter())
    monkeypatch.setattr(fix_duplicate, "build_checkpoint_gate", _NoOpCheckpointGate)

    loop = fix_duplicate._build_duplicate_per_file_loop()
    pipeline = SequentialAgent(
        name="test_duplicate_pipeline",
        sub_agents=[fix_duplicate.DuplicateFetchStep(), PerFileLoopStep(loop=loop)],
    )
    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.WORKING_DIR: str(git_repo), sk.LANGUAGE: "java-maven",
        sk.FILES_COMPLETED: [], sk.FILES_FLAGGED: [], sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.ISSUES_FIXED: [], sk.FILES_SINCE_CHECKPOINT: 0, sk.ORDERED_FILES_REMAINING: [],
    }
    _, final_state = _run_agent(pipeline, initial_state)

    assert final_state[sk.FILES_COMPLETED] == ["src/main/java/pkg/Foo.java"]
    assert final_state[sk.ISSUES_FIXED] == ["duplication:src/main/java/pkg/Foo.java"]
    assert "helper()" in (git_repo / "src/main/java/pkg/Foo.java").read_text()


# --- DuplicateBaselineStep -------------------------------------------------

def test_duplicate_baseline_step_captures_density_before(monkeypatch):
    monkeypatch.setattr(fix_duplicate, "get_metric_value", lambda *a, **kw: "12.5")
    initial_state = {sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t"}
    _, final_state = _run_agent(fix_duplicate.DuplicateBaselineStep(), initial_state)
    assert final_state["density_before"] == 12.5


def test_duplicate_baseline_step_handles_no_prior_analysis(monkeypatch):
    monkeypatch.setattr(fix_duplicate, "get_metric_value", lambda *a, **kw: None)
    initial_state = {sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t"}
    _, final_state = _run_agent(fix_duplicate.DuplicateBaselineStep(), initial_state)
    assert final_state["density_before"] is None


# --- DuplicateQualityGateStep -----------------------------------------------

def test_duplicate_quality_gate_escalates_when_nothing_completed():
    events, _ = _run_agent(fix_duplicate.DuplicateQualityGateStep(), {sk.FILES_COMPLETED: []})
    assert any(e.actions and e.actions.escalate for e in events)


def test_duplicate_quality_gate_escalates_when_rating_already_a(monkeypatch):
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "1.0"})
    monkeypatch.setattr(fix_duplicate, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.FILES_COMPLETED: ["Foo.java"],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    events, _ = _run_agent(fix_duplicate.DuplicateQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)


def test_duplicate_quality_gate_escalates_after_iteration_cap(monkeypatch):
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(fix_duplicate, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.FILES_COMPLETED: ["Foo.java"],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        "temp:duplicate_quality_iteration": 3,  # about to become 4, over the cap of 3
    }
    events, _ = _run_agent(fix_duplicate.DuplicateQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)


def test_duplicate_quality_gate_queues_new_smells_scoped_to_own_files(monkeypatch):
    """Regression guard for the actual feature request: only re-fix
    MAINTAINABILITY code smells Sonar found in files THIS duplication run
    refactored -- a different category, or a file this run never touched,
    is out of scope."""
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(fix_duplicate, "_scanned_branch", lambda s: "my-branch")

    issues = [
        {  # in scope: MAINTAINABILITY, on a file this run completed
            "category": "MAINTAINABILITY", "severity": "MINOR",
            "component_path": "src/main/java/pkg/Foo.java", "issue_key": "k1", "rule_key": "java:S1192",
            "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
        },
        {  # out of scope: different category
            "category": "SECURITY", "severity": "HIGH",
            "component_path": "src/main/java/pkg/Foo.java", "issue_key": "k2", "rule_key": "java:S1",
            "rule_name": "sec", "start_line": 1, "end_line": 1, "message": "m",
        },
        {  # out of scope: not a file this run touched
            "category": "MAINTAINABILITY", "severity": "MINOR",
            "component_path": "src/main/java/pkg/Other.java", "issue_key": "k3", "rule_key": "java:S1192",
            "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
        },
    ]
    monkeypatch.setattr(fix_duplicate.sonar_tools, "fetch_issues_and_hotspots", lambda *a, **kw: issues)
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_rule_description", lambda *a, **kw: "desc")

    initial_state = {
        sk.FILES_COMPLETED: ["src/main/java/pkg/Foo.java"],
        sk.FILES_FLAGGED: [], sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    _, final_state = _run_agent(fix_duplicate.DuplicateQualityGateStep(), initial_state)

    queue = final_state[sk.ORDERED_FILES_REMAINING]
    assert len(queue) == 1
    assert queue[0]["file"] == "src/main/java/pkg/Foo.java"
    assert [i["issue_key"] for i in queue[0]["issues"]] == ["k1"]
    assert queue[0]["issues"][0]["rule_description"] == "desc"


def test_duplicate_quality_gate_excludes_already_flagged_or_reverted_files(monkeypatch):
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "3.0"})
    monkeypatch.setattr(fix_duplicate, "_scanned_branch", lambda s: "my-branch")

    issues = [{
        "category": "MAINTAINABILITY", "severity": "MINOR",
        "component_path": "src/main/java/pkg/Foo.java", "issue_key": "k1", "rule_key": "java:S1192",
        "rule_name": "dup", "start_line": 1, "end_line": 1, "message": "m",
    }]
    monkeypatch.setattr(fix_duplicate.sonar_tools, "fetch_issues_and_hotspots", lambda *a, **kw: issues)
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_rule_description", lambda *a, **kw: "desc")

    initial_state = {
        sk.FILES_COMPLETED: ["src/main/java/pkg/Foo.java"],
        sk.FILES_FLAGGED: [{"file": "src/main/java/pkg/Foo.java", "reason": "already flagged earlier"}],
        sk.FILES_REVERTED_AT_CHECKPOINT: [],
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
    }
    events, _ = _run_agent(fix_duplicate.DuplicateQualityGateStep(), initial_state)
    assert any(e.actions and e.actions.escalate for e in events)


# --- DuplicateReportStep: before/after delta --------------------------------

def test_duplicate_report_shows_delta_and_maintainability_rating(monkeypatch):
    monkeypatch.setattr(fix_duplicate, "get_metric_value", lambda *a, **kw: "8.0")
    monkeypatch.setattr(fix_duplicate.sonar_tools, "get_quality_ratings", lambda *a, **kw: {"sqale_rating": "1.0"})
    monkeypatch.setattr(fix_duplicate, "_scanned_branch", lambda s: "my-branch")

    initial_state = {
        sk.SONAR_PROJECT_KEY: "proj", "sonar_base_url": "http://x", "sonar_token": "t",
        sk.BRANCH_NAME: "my-branch",
        sk.FILES_COMPLETED: ["Foo.java"], sk.ISSUES_FIXED: ["duplication:Foo.java"],
        sk.FILES_FLAGGED: [], sk.RUN_START_TIME: time.time(),
        sk.TOKEN_USAGE: {"prompt_tokens": 1, "candidates_tokens": 1, "total_tokens": 2},
        "temp:density_before": 30.0,
    }
    events, _ = _run_agent(fix_duplicate.DuplicateReportStep(), initial_state)
    text = next(e.content.parts[0].text for e in events if e.author == "duplicate_report_step")
    assert "30.0% → 8.0% (-22.0 pts)" in text  # density dropping is the good direction -- no stray "+"
    assert "Maintainability rating on this branch: A" in text

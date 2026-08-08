"""
Direct tests for the BaseAgent orchestration classes in agents.py -- the
part of the codebase test_agents.py's module docstring explicitly scopes
out ("need a real ADK InvocationContext"). Every bug found live this
session (NO_SAFE_FIX corruption, the S125 wrong-line miss, the
verification masking, the repeat-revert loop) lived here, not in the
pure-logic tools/*.py layer that already had coverage -- this file is a
deliberately bounded start at closing that gap, not exhaustive coverage
of every step class.

_run_agent() drives a real google.adk Runner + InMemorySessionService,
the same machinery run_local.py uses for the actual pipeline -- an
InvocationContext's internal fields (plugin_manager, agent_states,
credential_by_key, ...) are populated by the Runner itself and aren't
meant for manual construction, so this is the practical way to get a
real one rather than a fake/mocked object that might not behave like
the genuine thing.

No real network or LLM calls: any step under test that would call
fix_llm_agent has _build_fix_llm_agent monkeypatched to a stub agent
that yields a canned diff via state_delta, matching the real
LlmAgent's output_key contract. Any step that would call the real
Sonar API or a real Maven/Gradle build has those functions/adapters
monkeypatched too -- hermetic and fast, same philosophy as the rest of
this test suite.
"""

import asyncio
import subprocess
import uuid

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from sonar_autofix_agent import agents, state_schema as sk
from sonar_autofix_agent.adapters.base import BuildResult
from sonar_autofix_agent.agents import ApplyAndVerifyStep, FetchPrioritizeStep

APP_NAME = "test_app"


class _Seed(BaseAgent):
    """Seeds `data` into session state before the agent under test runs,
    in the SAME invocation -- required for any "temp:"-prefixed key
    (CURRENT_FILE_GROUP, PROPOSED_DIFF, ...): ADK's own
    extract_state_delta() silently drops temp:-prefixed keys from
    create_session()'s initial state param (by design -- it's the same
    mechanism behind this project's own "temp: never persisted"
    convention), so those keys can only be seeded from inside a real
    invocation, the same way the real pipeline's steps set them.

    Must go through an event's actions.state_delta, not direct
    ctx.session.state mutation -- confirmed live: direct mutation is
    visible to later steps within the SAME invocation (later steps read
    the same live dict), but the Runner's own persisted-state
    reconciliation (what get_session() returns afterward) only ever
    applies state_delta from yielded events via append_event, so a
    directly-mutated value that's never re-yielded as a delta vanishes
    the moment the invocation ends."""
    name: str = "seed"
    data: dict

    async def _run_async_impl(self, ctx):
        yield Event(author=self.name, actions=EventActions(state_delta=self.data))


class _Echo(BaseAgent):
    """Re-emits the ENTIRE live session state as one explicit state_delta
    after the agent under test runs. Needed for the same reason _Seed
    needs an explicit delta going in: a step that sets a state key via
    plain assignment (`s[key] = value`, as opposed to mutating an
    already-delta-seeded list/dict in place) only makes that value
    visible to LATER steps within the SAME invocation -- confirmed live
    against ReportStep's own final_report, which is exactly this bug
    (fixed alongside this file). Since this test harness's "invocation"
    ends right after the agent under test, without _Echo any fresh-key
    assignment would silently vanish from get_session()'s result, even
    though it's real, correct behavior for how these steps are actually
    used (deeply nested inside one much larger continuous invocation in
    production, where a later step DOES see it).

    Strips the "temp:" prefix from every key before echoing -- confirmed
    live that ADK's session-service commit path drops temp:-prefixed
    keys from EVERY event's state_delta, not just create_session()'s
    initial state param, so a temp: key genuinely cannot be observed via
    get_session() under any circumstance. Since production code (e.g.
    _retry_unresolved_issues) legitimately uses temp: for a same-
    invocation caller/callee handoff and that's real, correct behavior,
    not a bug -- this rename is a test-only observability trick, not a
    statement that these values are meant to be persisted for real."""
    name: str = "echo"

    async def _run_async_impl(self, ctx):
        renamed = {k.removeprefix("temp:"): v for k, v in ctx.session.state.items()}
        yield Event(author=self.name, actions=EventActions(state_delta=renamed))


def _run_agent(agent: BaseAgent, initial_state: dict) -> tuple[list[Event], dict]:
    """Returns (events, final_state). initial_state is seeded via a
    preceding step in the same invocation (see _Seed), not via
    create_session's state param -- see _Seed's docstring for why."""
    seeded = SequentialAgent(
        name="seeded_under_test", sub_agents=[_Seed(data=initial_state), agent, _Echo()],
    )

    async def _run():
        session_service = InMemorySessionService()
        user_id, session_id = "test_user", str(uuid.uuid4())
        await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        runner = Runner(agent=seeded, app_name=APP_NAME, session_service=session_service)
        trigger = types.Content(role="user", parts=[types.Part(text="start")])
        events = []
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=trigger):
            events.append(event)
        final_session = await session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        return events, final_session.state
    return asyncio.run(_run())


def _issue(issue_key="k1", rule_key="java:S4684", start_line=1, end_line=1):
    return {
        "issue_key": issue_key, "rule_key": rule_key, "rule_name": rule_key,
        "start_line": start_line, "end_line": end_line, "category": "SECURITY", "severity": "HIGH",
        "message": "test issue", "rule_description": "",
    }


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- ApplyAndVerifyStep: NO_SAFE_FIX on the first response -----------------

def test_no_safe_fix_flagged_without_touching_disk_or_compiling(tmp_path):
    """Regression: a NO_SAFE_FIX refusal used to fall through to apply_diff
    (safely rejected, since prose isn't valid diff syntax) and then a
    wasted full-file retry call that just refused again -- confirmed live,
    it wrote the refusal text to disk as if it were the file. This is the
    "caught on the very first response" path: no adapter/compile call
    should even happen."""
    group = {"file": "A.java", "issues": [_issue()]}
    initial_state = {
        sk.CURRENT_FILE_GROUP: group,
        sk.WORKING_DIR: str(tmp_path),
        sk.LANGUAGE: "java-maven",
        sk.PROPOSED_DIFF: "NO_SAFE_FIX: would break the public API contract",
        "temp:skip_llm_fix": False,
        sk.FILES_FLAGGED: [],
        sk.ISSUES_NO_SAFE_FIX: [],
        sk.ORDERED_FILES_REMAINING: [group],
        sk.CURRENT_FILE_CONTENT: "",
        sk.FILES_COMPLETED: [],
        sk.ISSUES_FIXED: [],
        sk.FILES_SINCE_CHECKPOINT: 0,
    }
    events, final_state = _run_agent(ApplyAndVerifyStep(), initial_state)

    assert final_state[sk.ISSUES_NO_SAFE_FIX] == ["k1"]
    assert len(final_state[sk.FILES_FLAGGED]) == 1
    assert final_state[sk.FILES_FLAGGED][0]["file"] == "A.java"
    assert "break the public API contract" in final_state[sk.FILES_FLAGGED][0]["reason"]
    assert final_state[sk.ORDERED_FILES_REMAINING] == []
    assert not (tmp_path / "A.java").exists()  # never written


# --- ApplyAndVerifyStep._retry_unresolved_issues ----------------------------

class _RetryHarness(BaseAgent):
    """Exercises ApplyAndVerifyStep._retry_unresolved_issues directly,
    sidestepping the full _run_async_impl flow (apply_diff, compile check,
    S2187 handling, verification) this test isn't targeting."""
    name: str = "retry_harness"
    group: dict
    working_dir: str
    unresolved_keys: list

    async def _run_async_impl(self, ctx):
        step = ApplyAndVerifyStep()
        async for event in step._retry_unresolved_issues(
            ctx, self.group, self.working_dir, self.unresolved_keys
        ):
            yield event


def _stub_fix_llm_agent(diff_text: str):
    class _Stub:
        async def run_async(self, ctx):
            ctx.session.state[sk.PROPOSED_DIFF] = diff_text
            yield Event(
                author="fix_llm_agent",
                actions=EventActions(state_delta={sk.PROPOSED_DIFF: diff_text}),
            )
    return _Stub()


def _fake_adapter(compile_passed: bool):
    class _FakeAdapter:
        def quick_compile_check(self, working_dir, scope):
            return BuildResult(passed=compile_passed, errors="" if compile_passed else "boom")
        def get_fix_prompt_addendum(self):
            return ""
    return _FakeAdapter()


def test_retry_unresolved_issues_resolves_a_genuine_miss(tmp_path, monkeypatch):
    """Regression: exact live bug (PetClinicRuntimeHints.java's S125 fix
    edited the wrong of two adjacent lines, twice in a row, with no
    automatic path back to a real re-attempt). A retry whose diff
    actually touches the flagged line, applies, and compiles should mark
    the issue resolved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    _git(["config", "user.email", "t@example.com"], str(repo))
    _git(["config", "user.name", "T"], str(repo))
    (repo / "A.java").write_text("class A {\n  int x = 1;\n}\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-m", "init"], str(repo))

    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,3 @@\n class A {\n-  int x = 1;\n+  int x = 2;\n }\n"
    monkeypatch.setattr(agents, "_build_fix_llm_agent", lambda: _stub_fix_llm_agent(diff))
    monkeypatch.setattr(agents, "get_adapter", lambda *a, **kw: _fake_adapter(compile_passed=True))

    group = {"file": "A.java", "issues": [_issue(end_line=2)]}
    initial_state = {
        sk.LANGUAGE: "java-maven",
        sk.CURRENT_FILE_GROUP: group,
    }
    _, final_state = _run_agent(
        _RetryHarness(group=group, working_dir=str(repo), unresolved_keys=["k1"]), initial_state,
    )
    assert final_state["retry_unresolved_ok"] == {"k1": True}
    assert "int x = 2;" in (repo / "A.java").read_text()


def test_retry_unresolved_issues_declines_via_no_safe_fix(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    _git(["config", "user.email", "t@example.com"], str(repo))
    _git(["config", "user.name", "T"], str(repo))
    (repo / "A.java").write_text("class A {\n  int x = 1;\n}\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-m", "init"], str(repo))

    monkeypatch.setattr(
        agents, "_build_fix_llm_agent",
        lambda: _stub_fix_llm_agent("NO_SAFE_FIX: needs a caller-side contract change"),
    )
    monkeypatch.setattr(agents, "get_adapter", lambda *a, **kw: _fake_adapter(compile_passed=True))

    group = {"file": "A.java", "issues": [_issue(end_line=2)]}
    initial_state = {sk.LANGUAGE: "java-maven", sk.CURRENT_FILE_GROUP: group}
    _, final_state = _run_agent(
        _RetryHarness(group=group, working_dir=str(repo), unresolved_keys=["k1"]), initial_state,
    )
    assert final_state["retry_unresolved_ok"] == {"k1": False}
    assert "caller-side contract change" in final_state["no_safe_fix_reason"]
    assert (repo / "A.java").read_text() == "class A {\n  int x = 1;\n}\n"  # untouched


def test_retry_unresolved_issues_reverts_on_compile_failure(tmp_path, monkeypatch):
    """A retry that applies but doesn't compile must leave the file
    exactly as it was before the retry, not partially patched."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    _git(["config", "user.email", "t@example.com"], str(repo))
    _git(["config", "user.name", "T"], str(repo))
    original = "class A {\n  int x = 1;\n}\n"
    (repo / "A.java").write_text(original)
    _git(["add", "-A"], str(repo))
    _git(["commit", "-m", "init"], str(repo))

    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,3 @@\n class A {\n-  int x = 1;\n+  int x = 2;\n }\n"
    monkeypatch.setattr(agents, "_build_fix_llm_agent", lambda: _stub_fix_llm_agent(diff))
    monkeypatch.setattr(agents, "get_adapter", lambda *a, **kw: _fake_adapter(compile_passed=False))

    group = {"file": "A.java", "issues": [_issue(end_line=2)]}
    initial_state = {sk.LANGUAGE: "java-maven", sk.CURRENT_FILE_GROUP: group}
    _, final_state = _run_agent(
        _RetryHarness(group=group, working_dir=str(repo), unresolved_keys=["k1"]), initial_state,
    )
    assert final_state["retry_unresolved_ok"] == {"k1": False}
    assert (repo / "A.java").read_text() == original


# --- FetchPrioritizeStep: FILES_REVERTED_AT_CHECKPOINT exclusion -----------

def test_fetch_prioritize_excludes_reverted_files(monkeypatch):
    """Regression: exact live bug (spring-petclinic's VisitController.java
    S4684 fix cascade-broke 6 files' tests, got bisect-reverted, then got
    reattempted identically on every subsequent outer_loop iteration --
    reverts only ever removed a file from FILES_COMPLETED, which
    FetchPrioritizeStep treats as "not yet done" rather than "already
    tried and failed"). A file in FILES_REVERTED_AT_CHECKPOINT must not
    reappear in ORDERED_FILES_REMAINING even though it's also absent from
    FILES_COMPLETED."""
    issues = [
        {
            "category": "SECURITY", "severity": "CRITICAL",
            "component_path": "Reverted.java", "issue_key": "r1", "rule_key": "java:S4684",
        },
        {
            "category": "SECURITY", "severity": "CRITICAL",
            "component_path": "Fresh.java", "issue_key": "f1", "rule_key": "java:S4684",
        },
    ]
    monkeypatch.setattr(agents.sonar_tools, "fetch_issues_and_hotspots", lambda *a, **kw: issues)
    monkeypatch.setattr(agents.sonar_tools, "get_rule_description", lambda *a, **kw: "desc")

    initial_state = {
        "sonar_base_url": "http://localhost:9000",
        sk.SONAR_PROJECT_KEY: "proj",
        "sonar_token": "tok",
        sk.WONT_FIX_REVIEW_QUEUE: [],
        sk.FILES_COMPLETED: [],
        sk.FILES_REVERTED_AT_CHECKPOINT: ["Reverted.java"],
    }
    _, final_state = _run_agent(FetchPrioritizeStep(), initial_state)

    remaining_files = [g["file"] for g in final_state[sk.ORDERED_FILES_REMAINING]]
    assert remaining_files == ["Fresh.java"]

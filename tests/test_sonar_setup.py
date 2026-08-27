"""Direct tests for sonar.setup.SetupStep's source_branch handling --
constructs a minimal fake ctx (SetupStep only ever touches
ctx.session.state) rather than the full Runner harness, since every other
collaborator (git, the adapter, the Sonar API) is stubbed anyway."""

import asyncio
from types import SimpleNamespace

import pytest

import sonar.setup as setup_mod
from core import state_schema as sk
from sonar.adapters import SonarPreflightError


class _FakeAdapter:
    def preflight_check(self, working_dir):
        pass

    def get_project_key(self, working_dir):
        return "proj"


def _drain(step, state):
    ctx = SimpleNamespace(session=SimpleNamespace(state=state))

    async def _run():
        events = []
        async for event in step._run_async_impl(ctx):
            events.append(event)
        return events
    return asyncio.run(_run())


def _base_state(tmp_path, **overrides):
    state = {
        "source": str(tmp_path), sk.SOURCE_TYPE: "local", sk.LANGUAGE: "java-maven",
        "sonar_base_url": "http://x", "sonar_token": "t", "timestamp": "20260101_000000",
    }
    state.update(overrides)
    return state


def test_setup_step_raises_when_source_branch_has_no_sonar_analysis(tmp_path, monkeypatch):
    """A source_branch that was never scanned on Sonar would otherwise let
    the run proceed, check out real code, and have the fetch step silently
    return zero issues with no explanation -- same class of bug
    check_project_analyzed already guards against for the no-analysis-at-
    all case, just scoped to one specific branch instead of the whole
    project."""
    monkeypatch.setattr(setup_mod.git_tools, "resolve_source", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(setup_mod, "get_adapter", lambda *a, **kw: _FakeAdapter())
    monkeypatch.setattr(setup_mod.sonar_tools, "validate_connection", lambda *a, **kw: None)
    monkeypatch.setattr(setup_mod.sonar_tools, "check_project_analyzed", lambda *a, **kw: None)
    monkeypatch.setattr(setup_mod.sonar_tools, "branch_exists", lambda *a, **kw: False)

    state = _base_state(tmp_path, source_branch="develop")
    with pytest.raises(SonarPreflightError, match="develop"):
        _drain(setup_mod.SetupStep(), state)


def test_setup_step_proceeds_when_source_branch_has_sonar_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_mod.git_tools, "resolve_source", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(setup_mod.git_tools, "create_branch", lambda *a, **kw: "proj_agent_x")
    monkeypatch.setattr(setup_mod, "get_adapter", lambda *a, **kw: _FakeAdapter())
    monkeypatch.setattr(setup_mod.sonar_tools, "validate_connection", lambda *a, **kw: None)
    monkeypatch.setattr(setup_mod.sonar_tools, "check_project_analyzed", lambda *a, **kw: None)
    monkeypatch.setattr(setup_mod.sonar_tools, "branch_exists", lambda *a, **kw: True)

    state = _base_state(tmp_path, source_branch="develop")
    events = _drain(setup_mod.SetupStep(), state)

    assert state[sk.BRANCH_NAME] == "proj_agent_x"
    assert any(
        "based on `develop`" in e.content.parts[0].text for e in events if e.content and e.content.parts
    )


def test_setup_step_skips_branch_check_when_no_source_branch_given(tmp_path, monkeypatch):
    """No source_branch -- the ordinary default-branch path -- must not
    even call branch_exists, let alone require it to return True."""
    monkeypatch.setattr(setup_mod.git_tools, "resolve_source", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(setup_mod.git_tools, "create_branch", lambda *a, **kw: "proj_agent_x")
    monkeypatch.setattr(setup_mod, "get_adapter", lambda *a, **kw: _FakeAdapter())
    monkeypatch.setattr(setup_mod.sonar_tools, "validate_connection", lambda *a, **kw: None)
    monkeypatch.setattr(setup_mod.sonar_tools, "check_project_analyzed", lambda *a, **kw: None)

    def _fail(*a, **kw):
        raise AssertionError("branch_exists should not be called when no source_branch was requested")
    monkeypatch.setattr(setup_mod.sonar_tools, "branch_exists", _fail)

    state = _base_state(tmp_path)
    _drain(setup_mod.SetupStep(), state)
    assert state[sk.BRANCH_NAME] == "proj_agent_x"

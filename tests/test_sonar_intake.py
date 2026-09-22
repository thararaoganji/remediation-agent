import asyncio
import uuid
from types import SimpleNamespace

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from core import state_schema as sk
from sonar.intake import build_intake_step, set_analysis_source

APP_NAME = "test_intake_app"


def _ctx():
    return SimpleNamespace(state={})


def test_set_analysis_source_stores_branch_when_given():
    ctx = _ctx()
    result = set_analysis_source("owner/repo", ctx, source_branch="develop")
    assert result["source_branch"] == "develop"
    assert ctx.state["source_branch"] == "develop"
    assert ctx.state["source"] == "owner/repo"
    assert ctx.state[sk.SOURCE_TYPE] == "github"


def test_set_analysis_source_strips_whitespace_from_branch():
    ctx = _ctx()
    set_analysis_source("owner/repo", ctx, source_branch="  release/v2  ")
    assert ctx.state["source_branch"] == "release/v2"


def test_set_analysis_source_defaults_branch_to_none_when_omitted():
    ctx = _ctx()
    result = set_analysis_source("owner/repo", ctx)
    assert ctx.state["source_branch"] is None
    assert result["source_branch"] == "(default branch)"


def test_set_analysis_source_treats_empty_branch_string_as_none():
    ctx = _ctx()
    set_analysis_source("owner/repo", ctx, source_branch="")
    assert ctx.state["source_branch"] is None


# --- build_intake_step: mid-run failures must reach the chat -----------------
#
# Driven through a real google.adk Runner (same machinery run_local.py and
# the real pipelines use) rather than a fake ctx, since build_intake_step's
# _run_async_impl calls pipeline.run_async(ctx) -- ADK's own wrapper, which
# needs a genuine InvocationContext, not a hand-built stand-in. Mirrors the
# harness in test_orchestration.py.

class _Seed(BaseAgent):
    name: str = "seed"
    data: dict

    async def _run_async_impl(self, ctx):
        yield Event(author=self.name, actions=EventActions(state_delta=self.data))


class _BoomPipeline(BaseAgent):
    """Stands in for a real pipeline hitting one of the RuntimeError/
    TimeoutError 'stop and explain' raise sites (a failed Sonar scan, a
    checkpoint that can't recover, a timed-out background task, ...)."""
    name: str = "boom_pipeline"

    async def _run_async_impl(self, ctx):
        raise RuntimeError("boom: sonar scan failed")
        yield  # pragma: no cover -- unreachable; keeps this an async generator


def _run_intake_step(pipeline: BaseAgent, initial_state: dict, monkeypatch) -> list[Event]:
    for key, value in {
        "GOOGLE_API_KEY": "x", "SONAR_BASE_URL": "http://x", "SONAR_TOKEN": "t", "LANGUAGE": "java",
    }.items():
        monkeypatch.setenv(key, value)

    step = build_intake_step(
        step_name="test_intake_step", description="test", welcome_message="hi",
        start_phrase="starting now", pipeline=pipeline, agent_slug="test",
    )
    seeded = SequentialAgent(name="seeded_under_test", sub_agents=[_Seed(data=initial_state), step])

    async def _run():
        session_service = InMemorySessionService()
        user_id, session_id = "test_user", str(uuid.uuid4())
        await session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        runner = Runner(agent=seeded, app_name=APP_NAME, session_service=session_service)
        trigger = types.Content(role="user", parts=[types.Part(text="start")])
        events = []
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=trigger):
            events.append(event)
        return events

    return asyncio.run(_run())


def _event_texts(events: list[Event]) -> list[str]:
    return [
        p.text for e in events if e.content
        for p in (e.content.parts or []) if getattr(p, "text", None)
    ]


def test_intake_step_turns_runtime_error_into_clean_stop_message(monkeypatch):
    """Regression: a RuntimeError from deep in the pipeline (a Sonar scan
    that never printed a CE task id, a checkpoint whose full build still
    failed after reverting the whole batch, ...) used to crash the entire
    run with no message reaching the chat at all -- adk web surfaced only
    a bare 'execution failed', with the actual reason visible nowhere.
    Confirmed live against a real tech-debt run."""
    events = _run_intake_step(
        _BoomPipeline(), {"source": "owner/repo", sk.SOURCE_TYPE: "github"}, monkeypatch,
    )
    assert any("Analysis stopped: boom: sonar scan failed" in t for t in _event_texts(events))


def test_intake_step_turns_timeout_error_into_clean_stop_message(monkeypatch):
    class _TimeoutPipeline(BaseAgent):
        name: str = "timeout_pipeline"

        async def _run_async_impl(self, ctx):
            raise TimeoutError("Sonar background task t1 did not finish within 600s")
            yield  # pragma: no cover

    events = _run_intake_step(
        _TimeoutPipeline(), {"source": "owner/repo", sk.SOURCE_TYPE: "github"}, monkeypatch,
    )
    assert any("Analysis stopped:" in t and "did not finish within 600s" in t for t in _event_texts(events))

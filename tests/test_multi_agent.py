import os
import tempfile
import pytest
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from sonar_duplicate_agent import root_agent as duplicate_agent
from sonar_coverage_agent import root_agent as coverage_agent
from sonar_autofix_agent import state_schema as sk
from sonar_autofix_agent.agents._shared import _msg

APP_NAME = "test_multi_agent"
USER_ID = "test_user"

@pytest.mark.anyio
async def test_duplicate_agent_flow(monkeypatch):
    # Mock set_analysis_source to succeed immediately and avoid LLM call
    initial_state = {
        "source": "/mock/path",
        "source_type": "local",
        "timestamp": "20260101_000000",
    }
    
    # Mock os.environ to have the required environment variables
    monkeypatch.setenv("GOOGLE_API_KEY", "mock_key")
    monkeypatch.setenv("SONAR_BASE_URL", "http://localhost:9000")
    monkeypatch.setenv("SONAR_TOKEN", "mock_token")
    monkeypatch.setenv("LANGUAGE", "java-maven")
    
    # Mock SetupStep so we do not actually try to clone or run git commands
    from sonar_autofix_agent.agents.setup import SetupStep
    async def mock_setup_run(self, ctx):
        ctx.session.state[sk.WORKING_DIR] = "/mock/path"
        ctx.session.state[sk.BRANCH_NAME] = "mock_branch_duplicate"
        ctx.session.state[sk.SONAR_PROJECT_KEY] = "mock_project"
        yield Event(author=self.name, content=_msg("Checked out branch mock_branch_duplicate"))
    monkeypatch.setattr(SetupStep, "_run_async_impl", mock_setup_run)
    
    # Mock PushStep to bypass actual git push
    from sonar_autofix_agent.agents.report import PushStep
    async def mock_push_run(self, ctx):
        ctx.session.state["temp:push_result"] = "pushed"
        yield Event(author=self.name, content=_msg("Mock pushed"))
    monkeypatch.setattr(PushStep, "_run_async_impl", mock_push_run)

    # Mock fetch_duplicated_files on the target module directly
    def mock_fetch_duplicated(sonar_base_url, project_key, token, branch):
        return [
            {"file": "src/main/java/com/example/Utils.java", "duplicated_lines_density": 45.2, "duplicated_blocks": 2}
        ]
    monkeypatch.setattr("sonar_duplicate_agent.fix_duplicate.fetch_duplicated_files", mock_fetch_duplicated)
    
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id="session_dup", state=initial_state
    )
    
    runner = Runner(agent=duplicate_agent, app_name=APP_NAME, session_service=session_service)
    trigger = types.Content(role="user", parts=[types.Part(text="start")])
    
    events = []
    async for event in runner.run_async(user_id=USER_ID, session_id="session_dup", new_message=trigger):
        events.append(event)
        
    state = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id="session_dup")
    
    # Assertions
    assert any("Mock pushed" in (getattr(e.content, "parts", [None])[0].text if getattr(e.content, "parts", None) else "") for e in events)
    assert any("Sonar Duplicate-Fix complete" in (getattr(e.content, "parts", [None])[0].text if getattr(e.content, "parts", None) else "") for e in events)
    assert "src/main/java/com/example/Utils.java" in state.state[sk.FILES_COMPLETED]
    
    final_event_text = events[-1].content.parts[0].text
    assert "Duplication Density rating: Improved to A" in final_event_text


@pytest.mark.anyio
async def test_coverage_agent_flow(monkeypatch):
    initial_state = {
        "source": "/mock/path",
        "source_type": "local",
        "timestamp": "20260101_000000",
    }
    
    monkeypatch.setenv("GOOGLE_API_KEY", "mock_key")
    monkeypatch.setenv("SONAR_BASE_URL", "http://localhost:9000")
    monkeypatch.setenv("SONAR_TOKEN", "mock_token")
    monkeypatch.setenv("LANGUAGE", "java-maven")
    
    from sonar_autofix_agent.agents.setup import SetupStep
    async def mock_setup_run(self, ctx):
        ctx.session.state[sk.WORKING_DIR] = "/mock/path"
        ctx.session.state[sk.BRANCH_NAME] = "mock_branch_coverage"
        ctx.session.state[sk.SONAR_PROJECT_KEY] = "mock_project"
        yield Event(author=self.name, content=_msg("Checked out branch mock_branch_coverage"))
    monkeypatch.setattr(SetupStep, "_run_async_impl", mock_setup_run)
    
    from sonar_autofix_agent.agents.report import PushStep
    async def mock_push_run(self, ctx):
        ctx.session.state["temp:push_result"] = "pushed"
        yield Event(author=self.name, content=_msg("Mock pushed"))
    monkeypatch.setattr(PushStep, "_run_async_impl", mock_push_run)

    # Mock fetch_uncovered_files on the target module directly
    def mock_fetch_uncovered(sonar_base_url, project_key, token, branch):
        return [
            {"file": "src/main/java/com/example/PaymentProcessor.java", "coverage": 72.5, "uncovered_lines": 15, "uncovered_conditions": 4}
        ]
    monkeypatch.setattr("sonar_coverage_agent.enhance_coverage.fetch_uncovered_files", mock_fetch_uncovered)
    
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id="session_cov", state=initial_state
    )
    
    runner = Runner(agent=coverage_agent, app_name=APP_NAME, session_service=session_service)
    trigger = types.Content(role="user", parts=[types.Part(text="start")])
    
    events = []
    async for event in runner.run_async(user_id=USER_ID, session_id="session_cov", new_message=trigger):
        events.append(event)
        
    state = await session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id="session_cov")
    
    assert any("Mock pushed" in (getattr(e.content, "parts", [None])[0].text if getattr(e.content, "parts", None) else "") for e in events)
    assert any("Sonar Coverage-Enhance complete" in (getattr(e.content, "parts", [None])[0].text if getattr(e.content, "parts", None) else "") for e in events)
    assert "src/test/java/com/example/PaymentProcessorTest.java" in state.state[sk.FILES_COMPLETED]
    
    final_event_text = events[-1].content.parts[0].text
    assert "Code Coverage: Increased by +4.8% (Rating: A)" in final_event_text

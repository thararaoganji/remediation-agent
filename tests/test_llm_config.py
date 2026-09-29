import pytest

from core import llm_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_VENDOR", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)


def test_default_vendor_is_google_with_the_given_default_model():
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "google"
    assert model == "gemini-3.7-flash"


def test_different_call_sites_keep_their_own_default_model():
    _, fix_model = llm_config.build_llm_model("gemini-3.7-flash")
    _, intake_model = llm_config.build_llm_model("gemini-flash-latest")
    assert fix_model == "gemini-3.7-flash"
    assert intake_model == "gemini-flash-latest"


def test_llm_model_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "google"
    assert model == "gemini-2.5-pro"


def test_openai_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "openai"
    assert isinstance(model, LiteLlm)
    assert model.model == "openai/gpt-4o"


def test_anthropic_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-3-5-sonnet-20241022")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "anthropic"
    assert isinstance(model, LiteLlm)
    assert model.model == "anthropic/claude-3-5-sonnet-20241022"


def test_mistral_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "mistral")
    monkeypatch.setenv("LLM_MODEL", "mistral-large-latest")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "mistral"
    assert isinstance(model, LiteLlm)
    assert model.model == "mistral/mistral-large-latest"


def test_groq_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "groq")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "groq"
    assert isinstance(model, LiteLlm)
    assert model.model == "groq/llama-3.3-70b-versatile"


def test_deepseek_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "deepseek"
    assert isinstance(model, LiteLlm)
    assert model.model == "deepseek/deepseek-chat"


def test_openrouter_vendor_returns_a_prefixed_lite_llm(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm

    monkeypatch.setenv("LLM_VENDOR", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-3.5-sonnet")
    vendor, model = llm_config.build_llm_model("gemini-3.7-flash")
    assert vendor == "openrouter"
    assert isinstance(model, LiteLlm)
    assert model.model == "openrouter/anthropic/claude-3.5-sonnet"


def test_required_env_var_follows_each_new_vendor(monkeypatch):
    for vendor, env_var in [
        ("mistral", "MISTRAL_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ]:
        monkeypatch.setenv("LLM_VENDOR", vendor)
        assert llm_config.required_env_var() == env_var


def test_unknown_vendor_raises_a_clear_error(monkeypatch):
    monkeypatch.setenv("LLM_VENDOR", "carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        llm_config.build_llm_model("gemini-3.7-flash")


def test_required_env_var_defaults_to_google(monkeypatch):
    assert llm_config.required_env_var() == "GOOGLE_API_KEY"


def test_required_env_var_follows_vendor(monkeypatch):
    monkeypatch.setenv("LLM_VENDOR", "anthropic")
    assert llm_config.required_env_var() == "ANTHROPIC_API_KEY"


def test_required_env_var_unknown_vendor_raises(monkeypatch):
    monkeypatch.setenv("LLM_VENDOR", "carrier-pigeon")
    with pytest.raises(ValueError, match="carrier-pigeon"):
        llm_config.required_env_var()

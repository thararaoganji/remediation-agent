"""Which LLM vendor/model an agent's LlmAgent calls talk to -- a plain
dict registry, same spirit as core/adapters/base.py's ADAPTER_REGISTRY:
the only thing that varies per vendor is the env var its key comes from
and how the model string gets wrapped, not enough to warrant a class
hierarchy.

"google" is ADK's native path -- LlmAgent talks to Gemini directly via
the google-genai SDK, which reads GOOGLE_API_KEY itself; a plain
"gemini-..." string is all `model=` needs. Every other vendor goes
through ADK's LiteLlm wrapper instead (see google-adk[extensions] in
requirements.txt), which expects a provider-prefixed model string
("openai/gpt-4o") and reads its own conventional env var.

LLM_VENDOR/LLM_MODEL env vars are optional everywhere they're read --
unset, every call site falls back to exactly its own pre-existing
hardcoded Gemini model, so a deployment that never sets them keeps
behaving exactly as before this module existed."""

import os

_VENDOR_ENV_VAR = {
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

DEFAULT_VENDOR = "google"


def _check_known(vendor: str) -> None:
    if vendor not in _VENDOR_ENV_VAR:
        raise ValueError(f"Unknown LLM vendor: {vendor!r} (known: {sorted(_VENDOR_ENV_VAR)})")


def required_env_var() -> str:
    """The env var name the currently-selected vendor (LLM_VENDOR, default
    "google") needs its API key in."""
    vendor = os.environ.get("LLM_VENDOR", DEFAULT_VENDOR)
    _check_known(vendor)
    return _VENDOR_ENV_VAR[vendor]


def build_llm_model(default_model: str):
    """Returns (vendor, model_for_llm_agent). `default_model` is what the
    caller would otherwise have hardcoded -- used as-is when LLM_MODEL
    isn't set, so each call site keeps its own pre-existing default."""
    vendor = os.environ.get("LLM_VENDOR", DEFAULT_VENDOR)
    _check_known(vendor)
    model_name = os.environ.get("LLM_MODEL") or default_model

    if vendor == "google":
        return vendor, model_name

    # Deferred: only needed (and only guaranteed installed) for non-Google
    # vendors -- see requirements.txt's google-adk[extensions].
    from google.adk.models.lite_llm import LiteLlm

    return vendor, LiteLlm(model=f"{vendor}/{model_name}")

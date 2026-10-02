"""Tests for local_secrets.py's Mongo-backed resolution -- a fake
pymongo-like client/db instead of a real Mongo connection, same shape as
test_run_status_local.py."""

import pytest

from core.tools import local_secrets
from core.tools.local_secrets import LocalSecretLookupError


class _FakeCollection:
    def __init__(self, docs=None):
        self._docs = docs or []

    def find_one(self, filter):
        for doc in self._docs:
            if all(doc.get(k) == v for k, v in filter.items()):
                return doc
        return None


class _FakeDB(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeCollection())


def _fake_db(sonar_servers=(), github_credentials=(), llm_configs=(), secrets=()):
    db = _FakeDB()
    db["sonar_servers"] = _FakeCollection(list(sonar_servers))
    db["github_credentials"] = _FakeCollection(list(github_credentials))
    db["llm_configs"] = _FakeCollection(list(llm_configs))
    db["_secrets"] = _FakeCollection(list(secrets))
    return db


SONAR = {"name": "My Sonar", "base_url": "http://localhost:9000", "ce_edition": True, "secret_name": "sonar-token-1"}
GITHUB = {"name": "My GitHub", "secret_name": "github-token-1"}
LLM_ACTIVE = {"vendor": "google", "model": "gemini-2.5-pro", "secret_name": "llm-api-key-1", "is_active": True}
SECRETS = [
    {"_id": "sonar-token-1", "versions": ["old-sonar-token", "sonar-token-value"]},
    {"_id": "github-token-1", "versions": ["github-token-value"]},
    {"_id": "llm-api-key-1", "versions": ["llm-key-value"]},
]


def test_resolve_env_sonar_only(monkeypatch):
    db = _fake_db(sonar_servers=[SONAR], llm_configs=[LLM_ACTIVE], secrets=SECRETS)
    monkeypatch.setattr(local_secrets, "_db", lambda: db)
    monkeypatch.setenv("SONAR_SERVER_NAME", "My Sonar")
    monkeypatch.delenv("GITHUB_CREDENTIAL_NAME", raising=False)

    env = local_secrets.resolve_env()

    assert env == {
        "SONAR_BASE_URL": "http://localhost:9000",
        "CE_EDITION": "true",
        "SONAR_TOKEN": "sonar-token-value",  # latest version, not the first
        "LLM_VENDOR": "google",
        "LLM_MODEL": "gemini-2.5-pro",
        "GOOGLE_API_KEY": "llm-key-value",
    }


def test_resolve_env_includes_github_when_credential_name_set(monkeypatch):
    db = _fake_db(sonar_servers=[SONAR], github_credentials=[GITHUB], llm_configs=[LLM_ACTIVE], secrets=SECRETS)
    monkeypatch.setattr(local_secrets, "_db", lambda: db)
    monkeypatch.setenv("SONAR_SERVER_NAME", "My Sonar")
    monkeypatch.setenv("GITHUB_CREDENTIAL_NAME", "My GitHub")

    env = local_secrets.resolve_env()

    assert env["GITHUB_TOKEN"] == "github-token-value"


def test_resolve_env_unknown_sonar_server_name_raises(monkeypatch):
    db = _fake_db(sonar_servers=[SONAR], llm_configs=[LLM_ACTIVE], secrets=SECRETS)
    monkeypatch.setattr(local_secrets, "_db", lambda: db)
    monkeypatch.setenv("SONAR_SERVER_NAME", "Nonexistent Sonar")

    with pytest.raises(LocalSecretLookupError, match="Nonexistent Sonar"):
        local_secrets.resolve_env()


def test_resolve_env_no_active_llm_config_raises(monkeypatch):
    db = _fake_db(sonar_servers=[SONAR], llm_configs=[], secrets=SECRETS)
    monkeypatch.setattr(local_secrets, "_db", lambda: db)
    monkeypatch.setenv("SONAR_SERVER_NAME", "My Sonar")

    with pytest.raises(LocalSecretLookupError, match="No active LLM config"):
        local_secrets.resolve_env()


def test_apply_to_environ_noop_without_cloud_provider_local(monkeypatch):
    monkeypatch.setattr(local_secrets, "resolve_env", lambda: {"SONAR_TOKEN": "should-not-be-used"})
    environ = {"SONAR_SERVER_NAME": "My Sonar"}  # CLOUD_PROVIDER not "local"

    local_secrets.apply_to_environ(environ)

    assert "SONAR_TOKEN" not in environ


def test_apply_to_environ_noop_without_sonar_server_name(monkeypatch):
    monkeypatch.setattr(local_secrets, "resolve_env", lambda: {"SONAR_TOKEN": "should-not-be-used"})
    environ = {"CLOUD_PROVIDER": "local"}  # no SONAR_SERVER_NAME

    local_secrets.apply_to_environ(environ)

    assert "SONAR_TOKEN" not in environ


def test_apply_to_environ_does_not_override_explicit_env_vars(monkeypatch):
    monkeypatch.setattr(local_secrets, "resolve_env", lambda: {"SONAR_TOKEN": "from-mongo", "GOOGLE_API_KEY": "from-mongo"})
    environ = {"CLOUD_PROVIDER": "local", "SONAR_SERVER_NAME": "My Sonar", "SONAR_TOKEN": "explicit-value"}

    local_secrets.apply_to_environ(environ)

    assert environ["SONAR_TOKEN"] == "explicit-value"
    assert environ["GOOGLE_API_KEY"] == "from-mongo"

"""Regression test for a real production bug: CosmosRunStatusReporter's
_get_client() constructed DefaultAzureCredential() with no
managed_identity_client_id hint, unlike every other Azure client in this
codebase (storage_azure.CosmosStore, job_runner_azure.ContainerAppsJobRunner,
secrets_azure). Since the job's identity is USER-assigned,
DefaultAzureCredential() can't resolve it without that hint -- and because
_update()'s bare `except Exception: pass` is deliberate (a status-reporting
hiccup must never break a real run), the failure was completely silent: the
job did its real work and exited 0, but every report_started/report_finished
write to Cosmos failed, leaving the dashboard showing "running" forever."""

from core.tools import run_status_azure
from core.tools.run_status_azure import CosmosRunStatusReporter


def test_get_client_passes_managed_identity_client_id(monkeypatch):
    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://example.documents.azure.com:443/")
    monkeypatch.setenv("AZURE_MANAGED_IDENTITY_CLIENT_ID", "identity-client-id-123")

    captured = {}

    class _FakeCredential:
        pass

    def _fake_default_azure_credential(**kwargs):
        captured.update(kwargs)
        return _FakeCredential()

    class _FakeCosmosClient:
        def __init__(self, url, credential):
            captured["url"] = url
            captured["credential"] = credential

    monkeypatch.setattr(run_status_azure, "DefaultAzureCredential", _fake_default_azure_credential)
    monkeypatch.setattr(run_status_azure, "CosmosClient", _FakeCosmosClient)

    reporter = CosmosRunStatusReporter()
    client = reporter._get_client()

    assert captured["managed_identity_client_id"] == "identity-client-id-123"
    assert isinstance(client, _FakeCosmosClient)


def test_get_client_returns_none_when_construction_raises(monkeypatch):
    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://example.documents.azure.com:443/")

    def _raise(**kwargs):
        raise Exception("no managed identity found")

    monkeypatch.setattr(run_status_azure, "DefaultAzureCredential", _raise)

    reporter = CosmosRunStatusReporter()
    assert reporter._get_client() is None


def test_get_client_returns_none_when_cosmos_package_missing(monkeypatch):
    monkeypatch.setattr(run_status_azure, "CosmosClient", None)

    reporter = CosmosRunStatusReporter()
    assert reporter._get_client() is None

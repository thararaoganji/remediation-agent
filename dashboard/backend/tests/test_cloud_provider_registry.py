"""Verifies the CLOUD_PROVIDER registry/factory mechanism itself (storage.py/
secrets.py/job_runner.py/event_stream.py) -- not the real GCP or Azure
backends' behavior against a live service, which needs real infra to
exercise meaningfully. This is what actually proves "adding a provider is
one class + one registry entry, nothing else changes" is true, and that an
unrecognized CLOUD_PROVIDER fails clearly rather than silently."""

import pytest

from app import event_stream, job_runner, secrets, storage
from app.event_stream_azure import CosmosEventStreamer
from app.event_stream_gcp import FirestoreEventStreamer
from app.job_runner_azure import ContainerAppsJobRunner
from app.job_runner_gcp import CloudRunJobRunner
from app.secrets_azure import KeyVaultStore
from app.secrets_gcp import SecretManagerStore
from app.storage_azure import CosmosStore
from app.storage_gcp import FirestoreStore


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    # Each facade caches its instance at module level once resolved --
    # reset between tests so CLOUD_PROVIDER changes actually take effect.
    monkeypatch.setattr(storage, "_instance", None)
    monkeypatch.setattr(secrets, "_instance", None)
    monkeypatch.setattr(job_runner, "_instance", None)
    monkeypatch.setattr(event_stream, "_instance", None)


def test_default_provider_is_gcp(monkeypatch):
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    assert isinstance(storage.get_storage(), FirestoreStore)
    assert isinstance(secrets.get_secret_store(), SecretManagerStore)
    assert isinstance(job_runner.get_job_runner(), CloudRunJobRunner)
    assert isinstance(event_stream.get_event_streamer(), FirestoreEventStreamer)


def test_azure_provider_resolves_to_azure_classes(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    assert isinstance(storage.get_storage(), CosmosStore)
    assert isinstance(secrets.get_secret_store(), KeyVaultStore)
    assert isinstance(job_runner.get_job_runner(), ContainerAppsJobRunner)
    assert isinstance(event_stream.get_event_streamer(), CosmosEventStreamer)


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    with pytest.raises(ValueError, match="aws"):
        storage.get_storage()
    with pytest.raises(ValueError, match="aws"):
        secrets.get_secret_store()
    with pytest.raises(ValueError, match="aws"):
        job_runner.get_job_runner()
    with pytest.raises(ValueError, match="aws"):
        event_stream.get_event_streamer()

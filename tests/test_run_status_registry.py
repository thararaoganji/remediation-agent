"""Verifies core/tools/run_status.py's CLOUD_PROVIDER registry/factory --
mirrors dashboard/backend/tests/test_cloud_provider_registry.py's reasoning
for the same mechanism at the root-agent-code layer."""

import pytest

from core.tools import run_status
from core.tools.run_status_azure import CosmosRunStatusReporter
from core.tools.run_status_gcp import FirestoreRunStatusReporter


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    monkeypatch.setattr(run_status, "_instance", None)


def test_default_provider_is_gcp(monkeypatch):
    monkeypatch.delenv("CLOUD_PROVIDER", raising=False)
    assert isinstance(run_status.get_run_status_reporter(), FirestoreRunStatusReporter)


def test_azure_provider_resolves_to_cosmos(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "azure")
    assert isinstance(run_status.get_run_status_reporter(), CosmosRunStatusReporter)


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    with pytest.raises(ValueError, match="aws"):
        run_status.get_run_status_reporter()

"""Secret Manager access for the dashboard backend -- one secret per
named Sonar server / GitHub credential (secret_name stored on each
Firestore doc, see firestore_db.py), plus the single global
google-api-key secret. Failures propagate as real exceptions (see
firestore_db.py's module docstring for why this differs from
core/tools/run_status.py's best-effort philosophy)."""

import os

from google.cloud import secretmanager

_client = None


def _get_client() -> secretmanager.SecretManagerServiceClient:
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()
    return _client


def _project_id() -> str:
    return os.environ["GCP_PROJECT_ID"]


def _secret_path(secret_id: str) -> str:
    return f"projects/{_project_id()}/secrets/{secret_id}"


def create_secret_with_value(secret_id: str, value: str) -> None:
    """Creates a brand-new secret AND its first version in one call --
    every caller here always has a value in hand at creation time (a
    Sonar server / GitHub credential is created with a token, never
    without one), so there's no use case for create-without-a-value."""
    client = _get_client()
    client.create_secret(
        parent=f"projects/{_project_id()}",
        secret_id=secret_id,
        secret={"replication": {"automatic": {}}},
    )
    add_secret_version(secret_id, value)


def add_secret_version(secret_id: str, value: str) -> None:
    _get_client().add_secret_version(
        parent=_secret_path(secret_id),
        payload={"data": value.encode("utf-8")},
    )


def access_secret_value(secret_id: str) -> str:
    response = _get_client().access_secret_version(name=f"{_secret_path(secret_id)}/versions/latest")
    return response.payload.data.decode("utf-8")


def secret_exists(secret_id: str) -> bool:
    try:
        _get_client().get_secret(name=_secret_path(secret_id))
        return True
    except Exception:
        return False


def delete_secret(secret_id: str) -> None:
    _get_client().delete_secret(name=_secret_path(secret_id))

"""Secret Manager SecretStore -- identical logic to the old
secret_manager.py, wrapped in a class so it satisfies secrets.SecretStore
alongside secrets_azure.KeyVaultStore."""

import os

from google.cloud import secretmanager

from .secrets import SecretStore


class SecretManagerStore(SecretStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> secretmanager.SecretManagerServiceClient:
        if self._client is None:
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _project_id(self) -> str:
        return os.environ["GCP_PROJECT_ID"]

    def _secret_path(self, secret_id: str) -> str:
        return f"projects/{self._project_id()}/secrets/{secret_id}"

    def create_secret_with_value(self, secret_id: str, value: str) -> None:
        self._get_client().create_secret(
            parent=f"projects/{self._project_id()}",
            secret_id=secret_id,
            secret={"replication": {"automatic": {}}},
        )
        self.add_secret_version(secret_id, value)

    def add_secret_version(self, secret_id: str, value: str) -> None:
        self._get_client().add_secret_version(
            parent=self._secret_path(secret_id),
            payload={"data": value.encode("utf-8")},
        )

    def access_secret_value(self, secret_id: str) -> str:
        response = self._get_client().access_secret_version(name=f"{self._secret_path(secret_id)}/versions/latest")
        return response.payload.data.decode("utf-8")

    def secret_exists(self, secret_id: str) -> bool:
        try:
            self._get_client().get_secret(name=self._secret_path(secret_id))
            return True
        except Exception:
            return False

    def delete_secret(self, secret_id: str) -> None:
        self._get_client().delete_secret(name=self._secret_path(secret_id))

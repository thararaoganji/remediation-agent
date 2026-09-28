"""Key Vault SecretStore -- Azure's direct equivalent to Secret Manager.
One structural difference from secrets_gcp.py: Key Vault has no separate
"create the secret" vs "add a version" operation -- set_secret() does both
in one call (creates on first use, auto-versions on every call after), so
create_secret_with_value/add_secret_version collapse to the same call
here. Auth is keyless via DefaultAzureCredential, same as storage_azure.py."""

import os

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from .secrets import SecretStore


class KeyVaultStore(SecretStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> SecretClient:
        if self._client is None:
            # managed_identity_client_id: see storage_azure.py's
            # CosmosStore._get_client -- this Container App has a
            # USER-assigned identity, which DefaultAzureCredential can't
            # resolve without an explicit client id hint.
            credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID"))
            self._client = SecretClient(vault_url=os.environ["AZURE_KEY_VAULT_URL"], credential=credential)
        return self._client

    def create_secret_with_value(self, secret_id: str, value: str) -> None:
        self._get_client().set_secret(secret_id, value)

    def add_secret_version(self, secret_id: str, value: str) -> None:
        self._get_client().set_secret(secret_id, value)

    def access_secret_value(self, secret_id: str) -> str:
        return self._get_client().get_secret(secret_id).value

    def secret_exists(self, secret_id: str) -> bool:
        try:
            self._get_client().get_secret(secret_id)
            return True
        except ResourceNotFoundError:
            return False

    def delete_secret(self, secret_id: str) -> None:
        self._get_client().begin_delete_secret(secret_id).result()

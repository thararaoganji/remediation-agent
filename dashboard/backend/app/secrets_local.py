"""MongoDB-backed SecretStore -- local-dev equivalent of secrets_gcp.py's
SecretManagerStore/secrets_azure.py's KeyVaultStore. Plaintext, no
encryption, no access control beyond "can you reach the compose network"
-- deliberately NOT a real secret store, just enough to let the dashboard's
create/rotate/access flows work end to end against a local database. Never
point this at anything other than the local `mongo` service in
docker-compose.yml.

Stored in its own `_secrets` collection (not a per-app one named
"secrets") so it can never collide with a real collection the dashboard
creates. Each doc is {_id: secret_id, versions: [v1, v2, ...]} -- an
append-only list standing in for Secret Manager's/Key Vault's own version
history; `access_secret_value` always returns the latest (last) one,
matching both real stores' "versions/latest" behavior."""

import os

from pymongo import MongoClient

from .secrets import SecretStore


class LocalSecretStore(SecretStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
        return self._client

    def _collection(self):
        db_name = os.environ.get("MONGO_DB", "dashboard")
        return self._get_client()[db_name]["_secrets"]

    def create_secret_with_value(self, secret_id: str, value: str) -> None:
        self._collection().replace_one({"_id": secret_id}, {"_id": secret_id, "versions": [value]}, upsert=True)

    def add_secret_version(self, secret_id: str, value: str) -> None:
        self._collection().update_one({"_id": secret_id}, {"$push": {"versions": value}}, upsert=True)

    def access_secret_value(self, secret_id: str) -> str:
        doc = self._collection().find_one({"_id": secret_id})
        if not doc or not doc.get("versions"):
            raise KeyError(f"No secret value found for {secret_id!r}")
        return doc["versions"][-1]

    def secret_exists(self, secret_id: str) -> bool:
        return self._collection().find_one({"_id": secret_id}) is not None

    def delete_secret(self, secret_id: str) -> None:
        self._collection().delete_one({"_id": secret_id})

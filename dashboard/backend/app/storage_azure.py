"""Cosmos DB (NoSQL API) DocumentStore -- Azure's closest equivalent to
Firestore: document-oriented, serverless billing (no idle cost). Every
container is provisioned ahead of time (see deploy/azure/) with partition
key /id, unlike Firestore's collections which need no provisioning at all
-- that's the one structural difference from storage_gcp.py this class has
to account for, everything else maps directly.

Auth is keyless: DefaultAzureCredential resolves to the Container App/Job's
attached managed identity in Azure, matching the same "no long-lived
credentials" approach the GCP docs already use for GitHub Actions'
Workload Identity Federation."""

import datetime
import os
from typing import Any

from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential

from .storage import DocumentStore

_DATABASE_NAME = "dashboard"


def _json_safe(value: Any) -> Any:
    """Recursively converts datetime objects to ISO 8601 strings. Cosmos's
    SDK just json.dumps()s whatever body it's given -- unlike Firestore's,
    which accepts native datetime objects and stores them as a proper
    Timestamp type -- so a raw `datetime.now(...)` anywhere in a doc (e.g.
    runs.py's create_run) 500s with "Object of type datetime is not JSON
    serializable" the moment it hits Cosmos. Every create_doc/update_doc
    caller in this app passes plain values straight from FastAPI/Pydantic,
    not already-JSON-safe data, so this walks the whole structure rather
    than just the top level -- fixing it here once, in the storage layer
    itself, rather than requiring every caller to remember Cosmos's
    limitation (storage.py's whole point is that callers shouldn't need
    to know which backend they're talking to)."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


class CosmosStore(DocumentStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> CosmosClient:
        if self._client is None:
            # managed_identity_client_id: this Container App/Job has a
            # USER-assigned identity, not a system-assigned one --
            # DefaultAzureCredential can't infer which identity to request
            # a token for without this hint (confirmed the hard way:
            # ManagedIdentityCredential failed with "configuration not
            # found in environment" despite the identity being correctly
            # attached and role-assigned).
            credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID"))
            self._client = CosmosClient(url=os.environ["AZURE_COSMOS_ENDPOINT"], credential=credential)
        return self._client

    def _container(self, collection: str):
        return self._get_client().get_database_client(_DATABASE_NAME).get_container_client(collection)

    def create_doc(self, collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
        import uuid

        doc_id = doc_id or str(uuid.uuid4())
        body = _json_safe({**data, "id": doc_id})
        self._container(collection).upsert_item(body=body)
        return doc_id

    def get_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        try:
            return self._container(collection).read_item(item=doc_id, partition_key=doc_id)
        except exceptions.CosmosResourceNotFoundError:
            return None

    def list_docs(
        self, collection: str, order_by: str | None = None, descending: bool = False
    ) -> list[dict[str, Any]]:
        docs = list(self._container(collection).read_all_items())
        if order_by:
            docs.sort(key=lambda d: d.get(order_by) or "", reverse=descending)
        return docs

    def update_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        # Cosmos's patch_item needs one JSON-Patch op per field; a
        # read-modify-write upsert is simpler and matches Firestore's
        # set(merge=True) semantics exactly (shallow field merge, creates
        # the doc if it didn't already exist).
        existing = self.get_doc(collection, doc_id) or {}
        merged = _json_safe({**existing, **data, "id": doc_id})
        self._container(collection).upsert_item(body=merged)

    def delete_doc(self, collection: str, doc_id: str) -> None:
        self._container(collection).delete_item(item=doc_id, partition_key=doc_id)

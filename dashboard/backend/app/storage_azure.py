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

import os
from typing import Any

from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential

from .storage import DocumentStore

_DATABASE_NAME = "dashboard"


class CosmosStore(DocumentStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> CosmosClient:
        if self._client is None:
            self._client = CosmosClient(url=os.environ["AZURE_COSMOS_ENDPOINT"], credential=DefaultAzureCredential())
        return self._client

    def _container(self, collection: str):
        return self._get_client().get_database_client(_DATABASE_NAME).get_container_client(collection)

    def create_doc(self, collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
        import uuid

        doc_id = doc_id or str(uuid.uuid4())
        body = {**data, "id": doc_id}
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
        merged = {**existing, **data, "id": doc_id}
        self._container(collection).upsert_item(body=merged)

    def delete_doc(self, collection: str, doc_id: str) -> None:
        self._container(collection).delete_item(item=doc_id, partition_key=doc_id)

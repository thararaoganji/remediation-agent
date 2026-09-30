"""MongoDB DocumentStore -- local-dev equivalent of storage_gcp.py's
FirestoreStore/storage_azure.py's CosmosStore, backed by the `mongo`
service in docker-compose.yml instead of a real cloud database. Exists so
the whole dashboard (and, via job_runner_local.py, real agent runs) can
run end to end on a laptop with no GCP/Azure account at all.

Uses doc_id as Mongo's own `_id` (a plain string, not an ObjectId) --
matches Firestore's/Cosmos's own "the id IS the primary key" model
directly, rather than introducing a second id scheme just for local dev."""

import os
import uuid
from typing import Any

from pymongo import MongoClient

from .storage import DocumentStore


def _strip_mongo_id(doc: dict[str, Any]) -> dict[str, Any]:
    # setdefault, not a plain assignment: most collections store nothing
    # but Mongo's own `_id` as the doc's identity, so this is what maps it
    # to the "id" field every DocumentStore implementation promises. But
    # event_stream_local.py's `events` collection keys `_id` as
    # "{run_id}:{event.id}" (to avoid cross-run collisions) while ALSO
    # storing the real event id in its own "id" field -- a plain
    # assignment here would clobber that correct value with the compound
    # key instead of just discarding it.
    doc = dict(doc)
    mongo_id = doc.pop("_id")
    doc.setdefault("id", mongo_id)
    return doc


class MongoStore(DocumentStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
        return self._client

    def _collection(self, name: str):
        db_name = os.environ.get("MONGO_DB", "dashboard")
        return self._get_client()[db_name][name]

    def create_doc(self, collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
        doc_id = doc_id or str(uuid.uuid4())
        self._collection(collection).replace_one({"_id": doc_id}, {**data, "_id": doc_id}, upsert=True)
        return doc_id

    def get_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._collection(collection).find_one({"_id": doc_id})
        return _strip_mongo_id(doc) if doc else None

    def list_docs(
        self, collection: str, order_by: str | None = None, descending: bool = False
    ) -> list[dict[str, Any]]:
        docs = [_strip_mongo_id(d) for d in self._collection(collection).find()]
        if order_by:
            docs.sort(key=lambda d: d.get(order_by) or "", reverse=descending)
        return docs

    def update_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._collection(collection).update_one({"_id": doc_id}, {"$set": data}, upsert=True)

    def delete_doc(self, collection: str, doc_id: str) -> None:
        self._collection(collection).delete_one({"_id": doc_id})

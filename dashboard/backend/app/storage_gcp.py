"""Firestore DocumentStore -- identical logic to the old firestore_db.py,
just wrapped in a class so it satisfies storage.DocumentStore alongside
storage_azure.CosmosStore.

list_docs() sorts client-side in Python rather than pushing order_by to
Firestore -- avoids needing composite indexes for every collection this
ever queries, a reasonable tradeoff at the scale this tool actually runs
at (an internal admin tool's run history: realistically dozens to low
thousands of docs, not a dataset that needs server-side pagination)."""

import os
import uuid
from typing import Any

from google.cloud import firestore

from .storage import DocumentStore


class FirestoreStore(DocumentStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> firestore.Client:
        if self._client is None:
            # Explicit project=, not left to auto-detection: firestore.Client()
            # only auto-detects from GOOGLE_CLOUD_PROJECT/gcloud config, not our
            # own GCP_PROJECT_ID (the name secrets_gcp.py/job_runner_gcp.py both
            # already read) -- without this, a correctly-set GCP_PROJECT_ID
            # still fails with "Project was not passed and could not be
            # determined from the environment."
            self._client = firestore.Client(project=os.environ["GCP_PROJECT_ID"])
        return self._client

    def create_doc(self, collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
        doc_id = doc_id or str(uuid.uuid4())
        self._get_client().collection(collection).document(doc_id).set(data)
        return doc_id

    def get_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self._get_client().collection(collection).document(doc_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        doc["id"] = snap.id
        return doc

    def list_docs(
        self, collection: str, order_by: str | None = None, descending: bool = False
    ) -> list[dict[str, Any]]:
        docs = []
        for snap in self._get_client().collection(collection).stream():
            doc = snap.to_dict()
            doc["id"] = snap.id
            docs.append(doc)
        if order_by:
            docs.sort(key=lambda d: d.get(order_by) or "", reverse=descending)
        return docs

    def update_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._get_client().collection(collection).document(doc_id).set(data, merge=True)

    def delete_doc(self, collection: str, doc_id: str) -> None:
        self._get_client().collection(collection).document(doc_id).delete()

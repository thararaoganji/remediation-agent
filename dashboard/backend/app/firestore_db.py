"""Firestore access for the dashboard backend -- named Sonar-server/
GitHub-credential configs and run history.

Unlike core/tools/run_status.py (best-effort, silent no-op on failure --
that module runs inside the agent container, where a status-reporting
hiccup must never break the actual remediation work), failures here
propagate as real exceptions: this is the backend API itself, so a broken
Firestore connection should surface as a 500 to the caller, not a silent
false success.

list_docs() sorts client-side in Python rather than pushing order_by to
Firestore -- avoids needing composite indexes for every collection this
ever queries, a reasonable tradeoff at the scale this tool actually runs
at (an internal admin tool's run history: realistically dozens to low
thousands of docs, not a dataset that needs server-side pagination)."""

import os
import uuid
from typing import Any

from google.cloud import firestore

_client = None


def _get_client() -> firestore.Client:
    global _client
    if _client is None:
        # Explicit project=, not left to auto-detection: firestore.Client()
        # only auto-detects from GOOGLE_CLOUD_PROJECT/gcloud config, not our
        # own GCP_PROJECT_ID (the name secret_manager.py/cloud_run.py both
        # already read) -- without this, a correctly-set GCP_PROJECT_ID
        # still fails with "Project was not passed and could not be
        # determined from the environment."
        _client = firestore.Client(project=os.environ["GCP_PROJECT_ID"])
    return _client


def create_doc(collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
    doc_id = doc_id or str(uuid.uuid4())
    _get_client().collection(collection).document(doc_id).set(data)
    return doc_id


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    snap = _get_client().collection(collection).document(doc_id).get()
    if not snap.exists:
        return None
    doc = snap.to_dict()
    doc["id"] = snap.id
    return doc


def list_docs(collection: str, order_by: str | None = None, descending: bool = False) -> list[dict[str, Any]]:
    docs = []
    for snap in _get_client().collection(collection).stream():
        doc = snap.to_dict()
        doc["id"] = snap.id
        docs.append(doc)
    if order_by:
        docs.sort(key=lambda d: d.get(order_by) or "", reverse=descending)
    return docs


def update_doc(collection: str, doc_id: str, data: dict[str, Any]) -> None:
    _get_client().collection(collection).document(doc_id).set(data, merge=True)


def delete_doc(collection: str, doc_id: str) -> None:
    _get_client().collection(collection).document(doc_id).delete()

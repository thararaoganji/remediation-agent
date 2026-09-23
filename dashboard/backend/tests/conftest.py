"""Fakes for Firestore/Secret Manager/Cloud Run, following the same
testable-module pattern as the main repo's core/tools/run_status.py and
core/adapters/jdk_provisioning.py: each real module exposes a
_get_client() that a fixture here monkeypatches to return a fake, rather
than a DI framework. No real GCP calls happen in this test suite."""

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("GCP_REGION", "us-central1")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret")

from app import auth, cloud_run, firestore_db, secret_manager  # noqa: E402
from app.main import app  # noqa: E402


# --- Fake Firestore ------------------------------------------------------

class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeChangeType:
    def __init__(self, name):
        self.name = name


class _FakeDocChange:
    def __init__(self, change_type_name, document):
        self.type = _FakeChangeType(change_type_name)
        self.document = document


class _FakeWatch:
    """Stands in for google.cloud.firestore_v1.watch.Watch -- tracks
    whether .unsubscribe() actually got called, so tests can assert on
    cleanup (e.g. event_stream.stream_run's finally block)."""

    def __init__(self, callback_list, callback):
        self._callback_list = callback_list
        self._callback = callback
        self.unsubscribed = False

    def unsubscribe(self):
        self.unsubscribed = True
        if self._callback in self._callback_list:
            self._callback_list.remove(self._callback)


class _FakeDocRef:
    def __init__(self, client, bucket, doc_id, path):
        self._client = client
        self._bucket = bucket
        self._doc_id = doc_id
        # Full path string (e.g. "runs/run-1" or "runs/run-1/events/evt-1")
        # -- the stable key on_snapshot() watches are registered/notified
        # under, since watches must survive this ref itself being a fresh,
        # short-lived object recreated on every .document(...) call (a
        # real Firestore client has no such per-call statefulness either).
        self._path = path

    def set(self, data, merge=False):
        existed = self._doc_id in self._bucket
        if merge and existed:
            self._bucket[self._doc_id].update(data)
        else:
            self._bucket[self._doc_id] = dict(data)
        snap = _FakeSnapshot(self._doc_id, self._bucket[self._doc_id])
        for cb in list(self._client._watches.get(self._path, [])):
            cb([snap], [], None)  # document-level watch on this exact doc
        parent_path = self._path.rsplit("/", 1)[0]
        change_type = "MODIFIED" if existed else "ADDED"
        for cb in list(self._client._watches.get(parent_path, [])):
            cb(None, [_FakeDocChange(change_type, snap)], None)  # collection-level watch on the parent

    def get(self):
        return _FakeSnapshot(self._doc_id, self._bucket.get(self._doc_id))

    def delete(self):
        self._bucket.pop(self._doc_id, None)

    def collection(self, name):
        sub_path = f"{self._path}/{name}"
        sub_store = self._client._sub_stores.setdefault(sub_path, {})
        return _FakeCollectionRef(self._client, {name: sub_store}, name, path=sub_path)

    def on_snapshot(self, callback):
        callback([self.get()], [], None)  # initial snapshot, matches real Firestore
        self._client._watches.setdefault(self._path, []).append(callback)
        return _FakeWatch(self._client._watches[self._path], callback)


class _FakeCollectionRef:
    def __init__(self, client, store, name, path):
        self._client = client
        self._store = store
        self._name = name
        self._path = path  # e.g. "runs" or "runs/run-1/events"

    def document(self, doc_id):
        bucket = self._store.setdefault(self._name, {})
        return _FakeDocRef(self._client, bucket, doc_id, path=f"{self._path}/{doc_id}")

    def order_by(self, field):
        return self  # ordering doesn't affect this fake's correctness

    def stream(self):
        bucket = self._store.get(self._name, {})
        return [_FakeSnapshot(doc_id, data) for doc_id, data in bucket.items()]

    def on_snapshot(self, callback):
        initial_changes = [_FakeDocChange("ADDED", snap) for snap in self.stream()]
        callback(None, initial_changes, None)  # initial snapshot, matches real Firestore
        self._client._watches.setdefault(self._path, []).append(callback)
        return _FakeWatch(self._client._watches[self._path], callback)


class FakeFirestoreClient:
    def __init__(self):
        self.store: dict = {}
        self._sub_stores: dict = {}  # subcollection path -> {doc_id: fields}, shared across .document() calls
        self._watches: dict = {}     # path -> [callback, ...]

    def collection(self, name):
        return _FakeCollectionRef(self, self.store, name, path=name)


# --- Fake Secret Manager --------------------------------------------------

class FakeSecretManagerClient:
    def __init__(self):
        self.secrets: dict[str, list[bytes]] = {}

    def create_secret(self, parent, secret_id, secret):
        if secret_id in self.secrets:
            raise Exception(f"secret {secret_id} already exists")
        self.secrets[secret_id] = []

    def add_secret_version(self, parent, payload):
        secret_id = parent.rsplit("/", 1)[-1]
        if secret_id not in self.secrets:
            raise Exception(f"secret {secret_id} not found")
        self.secrets[secret_id].append(payload["data"])

    def access_secret_version(self, name):
        secret_id = name.split("/secrets/")[1].split("/versions/")[0]
        versions = self.secrets.get(secret_id)
        if not versions:
            raise Exception("secret version not found")
        return types.SimpleNamespace(payload=types.SimpleNamespace(data=versions[-1]))

    def get_secret(self, name):
        secret_id = name.rsplit("/", 1)[-1]
        if secret_id not in self.secrets:
            raise Exception("secret not found")
        return types.SimpleNamespace(name=name)

    def delete_secret(self, name):
        secret_id = name.rsplit("/", 1)[-1]
        self.secrets.pop(secret_id, None)


# --- Fake Cloud Run --------------------------------------------------------

class FakeCloudRunClient:
    def __init__(self, should_fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.should_fail = should_fail

    def run_job(self, request):
        if self.should_fail:
            raise Exception("simulated Cloud Run API error")
        env = {e.name: e.value for e in request.overrides.container_overrides[0].env}
        self.calls.append((request.name, env))
        execution_name = f"{request.name}/executions/fake-execution-1"
        return types.SimpleNamespace(metadata=types.SimpleNamespace(name=execution_name))


# --- Fixtures --------------------------------------------------------------

@pytest.fixture
def fake_firestore(monkeypatch):
    client = FakeFirestoreClient()
    monkeypatch.setattr(firestore_db, "_get_client", lambda: client)
    return client


@pytest.fixture
def fake_secrets(monkeypatch):
    client = FakeSecretManagerClient()
    monkeypatch.setattr(secret_manager, "_get_client", lambda: client)
    return client


@pytest.fixture
def fake_cloud_run(monkeypatch):
    client = FakeCloudRunClient()
    monkeypatch.setattr(cloud_run, "_get_client", lambda: client)
    return client


@pytest.fixture
def client(fake_firestore, fake_secrets, fake_cloud_run):
    return TestClient(app)


# --- Auth test helper --------------------------------------------------

_TEST_PASSWORD = "test-password-123"


def login_as(client, fake_firestore, email, role="user"):
    """Seeds a users/{email} doc directly into the fake store and logs in
    through the real /api/auth/login endpoint (exercises the real
    hashing/cookie code path, not a shortcut). TestClient persists cookies
    across calls on the same instance, so every subsequent call this
    `client` makes is authenticated as this user."""
    fake_firestore.collection("users").document(email).set({
        "email": email, "password_hash": auth.hash_password(_TEST_PASSWORD), "role": role,
    })
    resp = client.post("/api/auth/login", json={"email": email, "password": _TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return client

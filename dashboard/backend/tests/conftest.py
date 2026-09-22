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

from app import cloud_run, firestore_db, secret_manager  # noqa: E402
from app.main import app  # noqa: E402


# --- Fake Firestore ------------------------------------------------------

class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, bucket, doc_id):
        self._bucket = bucket
        self._doc_id = doc_id

    def set(self, data, merge=False):
        if merge and self._doc_id in self._bucket:
            self._bucket[self._doc_id].update(data)
        else:
            self._bucket[self._doc_id] = dict(data)

    def get(self):
        return _FakeSnapshot(self._doc_id, self._bucket.get(self._doc_id))

    def delete(self):
        self._bucket.pop(self._doc_id, None)


class _FakeCollectionRef:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        bucket = self._store.setdefault(self._name, {})
        return _FakeDocRef(bucket, doc_id)

    def stream(self):
        bucket = self._store.get(self._name, {})
        return [_FakeSnapshot(doc_id, data) for doc_id, data in bucket.items()]


class FakeFirestoreClient:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name):
        return _FakeCollectionRef(self.store, name)


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

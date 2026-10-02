"""Tests for LocalSecretStore's at-rest encryption -- same fake-client
pattern as test_storage_local.py (no real Mongo needed). Covers the actual
point of this change (stored bytes aren't the plaintext value, round-trips
correctly) and the failure mode a wrong/changed LOCAL_SECRETS_ENCRYPTION_KEY
produces (a clear KeyError, not silent corruption of the stored ciphertext
-- see secrets_local.py's access_secret_value docstring for why that
distinction matters)."""

import pytest

from app import secrets_local
from app.secrets_local import LocalSecretStore


class _FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    def replace_one(self, filter, replacement, upsert=False):
        self.docs[filter["_id"]] = dict(replacement)

    def find_one(self, filter):
        return self.docs.get(filter["_id"])

    def update_one(self, filter, update, upsert=False):
        doc = self.docs.setdefault(filter["_id"], {"_id": filter["_id"], "versions": []})
        if "$push" in update:
            doc.setdefault("versions", []).append(update["$push"]["versions"])

    def delete_one(self, filter):
        self.docs.pop(filter["_id"], None)


class _FakeDB(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeCollection())


class _FakeClient(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeDB())


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(secrets_local, "MongoClient", lambda url: _FakeClient())
    return LocalSecretStore()


def test_stored_value_is_not_plaintext(store):
    store.create_secret_with_value("sonar-token-1", "sk-real-token-value")

    raw = store._collection().find_one({"_id": "sonar-token-1"})

    assert "sk-real-token-value" not in raw["versions"][0]


def test_access_secret_value_round_trips(store):
    store.create_secret_with_value("sonar-token-1", "sk-real-token-value")

    assert store.access_secret_value("sonar-token-1") == "sk-real-token-value"


def test_add_secret_version_appends_and_access_returns_latest(store):
    store.create_secret_with_value("sonar-token-1", "v1")
    store.add_secret_version("sonar-token-1", "v2")

    raw = store._collection().find_one({"_id": "sonar-token-1"})
    assert len(raw["versions"]) == 2
    assert store.access_secret_value("sonar-token-1") == "v2"


def test_access_secret_value_missing_raises_key_error(store):
    with pytest.raises(KeyError):
        store.access_secret_value("nonexistent")


def test_access_secret_value_with_changed_key_raises_clear_error(store, monkeypatch):
    store.create_secret_with_value("sonar-token-1", "sk-real-token-value")

    monkeypatch.setenv("LOCAL_SECRETS_ENCRYPTION_KEY", "a-completely-different-key")

    with pytest.raises(KeyError, match="Could not decrypt"):
        store.access_secret_value("sonar-token-1")


def test_two_different_passphrases_derive_different_fernet_keys(monkeypatch):
    # Black-box check (no reliance on Fernet's internal attributes): a
    # token encrypted under one passphrase must fail to decrypt under a
    # different one, proving _fernet() actually derives distinct keys
    # rather than e.g. falling back to some shared default on a typo.
    monkeypatch.delenv("LOCAL_SECRETS_ENCRYPTION_KEY", raising=False)
    token = secrets_local._fernet().encrypt(b"value")

    monkeypatch.setenv("LOCAL_SECRETS_ENCRYPTION_KEY", "something-else")
    with pytest.raises(secrets_local.InvalidToken):
        secrets_local._fernet().decrypt(token)

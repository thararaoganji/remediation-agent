"""Tests for MongoStore -- same fake-client pattern as test_storage_azure.py
(no real Mongo needed). Includes a regression test for a real bug caught
while writing this: _strip_mongo_id's original implementation clobbered
an already-correct "id" field (event_stream_local.py's events, keyed by
the compound "{run_id}:{event_id}" as Mongo's own _id) with that compound
key instead of leaving the real event id alone."""

from app.storage_local import MongoStore, _strip_mongo_id


class _FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    def replace_one(self, filter, replacement, upsert=False):
        self.docs[filter["_id"]] = dict(replacement)

    def find_one(self, filter):
        return self.docs.get(filter["_id"])

    def find(self):
        return list(self.docs.values())

    def update_one(self, filter, update, upsert=False):
        doc = self.docs.setdefault(filter["_id"], {"_id": filter["_id"]})
        doc.update(update["$set"])

    def delete_one(self, filter):
        self.docs.pop(filter["_id"], None)


class _FakeDB(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeCollection())


class _FakeClient(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeDB())


def _store_with_fake_client():
    store = MongoStore()
    fake_client = _FakeClient()
    store._get_client = lambda: fake_client
    return store, fake_client


def test_strip_mongo_id_maps_id_for_docs_with_no_separate_id_field():
    assert _strip_mongo_id({"_id": "run-1", "status": "running"}) == {"id": "run-1", "status": "running"}


def test_strip_mongo_id_preserves_an_existing_id_field():
    # Regression: events are keyed by a compound "_id" ("{run_id}:{event_id}")
    # but carry the REAL event id in their own "id" field -- stripping must
    # never let the compound key clobber it.
    doc = {"_id": "run-1:evt-1", "id": "evt-1", "run_id": "run-1", "author": "fix_llm_agent"}
    result = _strip_mongo_id(doc)
    assert result["id"] == "evt-1"
    assert "_id" not in result


def test_create_doc_generates_id_when_not_given():
    store, client = _store_with_fake_client()
    doc_id = store.create_doc("sonar_servers", {"name": "Prod"})
    assert doc_id
    assert client["dashboard"]["sonar_servers"].docs[doc_id]["name"] == "Prod"


def test_create_doc_uses_given_id():
    store, client = _store_with_fake_client()
    doc_id = store.create_doc("runs", {"status": "queued"}, doc_id="run-1")
    assert doc_id == "run-1"


def test_get_doc_returns_none_for_missing():
    store, _ = _store_with_fake_client()
    assert store.get_doc("runs", "does-not-exist") is None


def test_get_doc_round_trips_with_id_field():
    store, _ = _store_with_fake_client()
    store.create_doc("runs", {"status": "running"}, doc_id="run-1")
    doc = store.get_doc("runs", "run-1")
    assert doc == {"id": "run-1", "status": "running"}


def test_update_doc_merges_not_replaces():
    store, _ = _store_with_fake_client()
    store.create_doc("runs", {"status": "queued", "agent_type": "techdebt"}, doc_id="run-1")
    store.update_doc("runs", "run-1", {"status": "running"})
    doc = store.get_doc("runs", "run-1")
    assert doc["status"] == "running"
    assert doc["agent_type"] == "techdebt"


def test_delete_doc_removes_it():
    store, _ = _store_with_fake_client()
    store.create_doc("runs", {"status": "queued"}, doc_id="run-1")
    store.delete_doc("runs", "run-1")
    assert store.get_doc("runs", "run-1") is None


def test_list_docs_sorts_by_order_by():
    store, _ = _store_with_fake_client()
    store.create_doc("runs", {"created_at": "2026-09-28"}, doc_id="run-1")
    store.create_doc("runs", {"created_at": "2026-09-29"}, doc_id="run-2")
    docs = store.list_docs("runs", order_by="created_at", descending=True)
    assert [d["id"] for d in docs] == ["run-2", "run-1"]

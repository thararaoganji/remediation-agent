"""Regression test for a real production bug: a raw `datetime` anywhere in a
doc passed to CosmosStore.create_doc/update_doc 500'd with "Object of type
datetime is not JSON serializable" (Cosmos's SDK just json.dumps()s the body
directly, unlike Firestore's, which accepts native datetime objects). Fixed
in the storage layer itself (_json_safe), not by hunting down every caller,
so this only needs to prove the storage layer's behavior -- not exercise a
real Cosmos account."""

from datetime import datetime, timezone

from app.storage_azure import CosmosStore, _json_safe


def test_json_safe_converts_top_level_datetime():
    now = datetime(2026, 9, 28, 13, 52, 7, tzinfo=timezone.utc)
    assert _json_safe({"created_at": now}) == {"created_at": now.isoformat()}


def test_json_safe_converts_nested_datetime():
    now = datetime(2026, 9, 28, 13, 52, 7, tzinfo=timezone.utc)
    data = {"final_report": {"finished_at": now, "steps": [{"ran_at": now}]}}
    result = _json_safe(data)
    assert result["final_report"]["finished_at"] == now.isoformat()
    assert result["final_report"]["steps"][0]["ran_at"] == now.isoformat()


def test_json_safe_leaves_non_datetime_values_unchanged():
    data = {"status": "queued", "retries": 0, "tags": ["a", "b"], "final_report": None}
    assert _json_safe(data) == data


def test_create_doc_stores_json_safe_body(monkeypatch):
    store = CosmosStore()
    captured = {}

    class _FakeContainer:
        def upsert_item(self, body):
            captured["body"] = body

    monkeypatch.setattr(store, "_container", lambda collection: _FakeContainer())

    now = datetime(2026, 9, 28, 13, 52, 7, tzinfo=timezone.utc)
    store.create_doc("runs", {"status": "queued", "created_at": now}, doc_id="run-1")

    assert captured["body"]["created_at"] == now.isoformat()
    assert captured["body"]["id"] == "run-1"


def test_update_doc_stores_json_safe_merged_body(monkeypatch):
    store = CosmosStore()
    captured = {}

    class _FakeContainer:
        def upsert_item(self, body):
            captured["body"] = body

    monkeypatch.setattr(store, "get_doc", lambda collection, doc_id: {"status": "queued", "id": doc_id})
    monkeypatch.setattr(store, "_container", lambda collection: _FakeContainer())

    now = datetime(2026, 9, 28, 13, 55, 0, tzinfo=timezone.utc)
    store.update_doc("runs", "run-1", {"status": "finished", "finished_at": now})

    assert captured["body"]["finished_at"] == now.isoformat()
    assert captured["body"]["status"] == "finished"

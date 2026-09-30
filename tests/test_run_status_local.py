"""Tests for MongoRunStatusReporter -- same shape as test_run_status_azure.py,
a fake pymongo-like client instead of a real Mongo connection."""

from core.tools import run_status_local
from core.tools.run_status_local import MongoRunStatusReporter


class _FakeCollection:
    def __init__(self):
        self.docs: dict = {}

    def update_one(self, filter, update, upsert=False):
        doc = self.docs.setdefault(filter["_id"], {"_id": filter["_id"]})
        doc.update(update["$set"])


class _FakeDB(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeCollection())


class _FakeClient(dict):
    def __getitem__(self, name):
        return self.setdefault(name, _FakeDB())


def test_get_client_reads_mongo_url(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    captured = {}

    def _fake_mongo_client(url):
        captured["url"] = url
        return _FakeClient()

    monkeypatch.setattr(run_status_local, "MongoClient", _fake_mongo_client)

    reporter = MongoRunStatusReporter()
    client = reporter._get_client()

    assert captured["url"] == "mongodb://mongo:27017"
    assert isinstance(client, _FakeClient)


def test_get_client_returns_none_when_mongo_url_missing(monkeypatch):
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.setattr(run_status_local, "MongoClient", lambda url: _FakeClient())

    reporter = MongoRunStatusReporter()
    assert reporter._get_client() is None


def test_get_client_returns_none_when_pymongo_package_missing(monkeypatch):
    monkeypatch.setattr(run_status_local, "MongoClient", None)
    reporter = MongoRunStatusReporter()
    assert reporter._get_client() is None


def test_report_started_writes_expected_fields(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    fake_client = _FakeClient()
    monkeypatch.setattr(run_status_local, "MongoClient", lambda url: fake_client)

    reporter = MongoRunStatusReporter()
    reporter.report_started("run-1", "techdebt", "github", "owner/repo", "main")

    doc = fake_client["dashboard"]["runs"].docs["run-1"]
    assert doc["agent_type"] == "techdebt"
    assert doc["status"] == "running"
    assert "started_at" in doc


def test_report_finished_writes_final_report(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    fake_client = _FakeClient()
    monkeypatch.setattr(run_status_local, "MongoClient", lambda url: fake_client)

    reporter = MongoRunStatusReporter()
    reporter.report_finished("run-1", "succeeded", final_report={"branch_name": "x"})

    doc = fake_client["dashboard"]["runs"].docs["run-1"]
    assert doc["status"] == "succeeded"
    assert doc["final_report"] == {"branch_name": "x"}


def test_report_event_keys_by_compound_id_and_keeps_real_id(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    fake_client = _FakeClient()
    monkeypatch.setattr(run_status_local, "MongoClient", lambda url: fake_client)

    class _FakeEvent:
        id = "evt-1"

        def model_dump(self, **kw):
            return {"author": "fix_llm_agent"}

    reporter = MongoRunStatusReporter()
    reporter.report_event("run-1", _FakeEvent())

    events = fake_client["dashboard"]["events"].docs
    assert "run-1:evt-1" in events
    assert events["run-1:evt-1"]["id"] == "evt-1"
    assert events["run-1:evt-1"]["run_id"] == "run-1"


def test_report_started_noop_when_run_id_none(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")

    def _fail(*a, **kw):
        raise AssertionError("MongoClient should not have been touched")

    monkeypatch.setattr(run_status_local, "MongoClient", _fail)

    reporter = MongoRunStatusReporter()
    reporter.report_started(None, "techdebt", "github", "owner/repo")


def test_update_swallows_exceptions():
    class _ExplodingCollection:
        def update_one(self, *a, **kw):
            raise Exception("connection refused")

    class _ExplodingDB(dict):
        def __getitem__(self, name):
            return _ExplodingCollection()

    class _ExplodingClient(dict):
        def __getitem__(self, name):
            return _ExplodingDB()

    reporter = MongoRunStatusReporter()
    reporter._get_client = lambda: _ExplodingClient()
    # Must not raise -- a Mongo hiccup can never fail the actual run.
    reporter.report_started("run-1", "techdebt", "github", "owner/repo")

from core.tools import run_status


class _FakeDocRef:
    def __init__(self, store, doc_id, subcollection_registry):
        self._store = store
        self._doc_id = doc_id
        # Shared dict living on the _FakeClient itself, keyed by
        # (doc_id, subcollection_name) -- NOT owned by this ref or by
        # whichever _FakeCollection produced it, both of which are
        # recreated fresh on every .collection()/.document() call. Owning
        # it here would silently lose subcollection data the moment
        # anything re-navigates via a fresh .collection("runs") call, as
        # a real Firestore client (which has no such per-call statefulness
        # at all) never would.
        self._subcollection_registry = subcollection_registry

    def set(self, fields, merge=True):
        assert merge is True  # every write must merge, never clobber earlier fields
        self._store.setdefault(self._doc_id, {}).update(fields)

    def collection(self, name):
        key = (self._doc_id, name)
        sub_store = self._subcollection_registry.setdefault(key, {})
        return _FakeCollection(sub_store, self._subcollection_registry)


class _FakeCollection:
    def __init__(self, store, subcollection_registry=None):
        self._store = store
        self._subcollection_registry = subcollection_registry if subcollection_registry is not None else {}

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id, self._subcollection_registry)


class _FakeClient:
    def __init__(self):
        self.store: dict = {}
        self._subcollection_registry: dict = {}

    def collection(self, name):
        assert name == "runs"
        return _FakeCollection(self.store, self._subcollection_registry)

    def events_store(self, run_id: str) -> dict:
        """Test helper -- the flat {event_id: fields} store for one run's
        events subcollection, however many separate .collection("runs")
        calls were made to reach it."""
        return self._subcollection_registry.get((run_id, "events"), {})


def _fail_if_called(*a, **kw):
    raise AssertionError("Firestore should not have been touched")


class _FakeEvent:
    """Stands in for a real google.adk.events.Event -- just enough surface
    (an .id and a Pydantic-style .model_dump()) for report_event(), without
    depending on constructing a real ADK Event in a unit test."""

    def __init__(self, event_id="evt-1", author="fix_llm_agent", text="did a thing"):
        self.id = event_id
        self._author = author
        self._text = text

    def model_dump(self, mode="json", exclude_none=True):
        return {
            "id": self.id,
            "author": self._author,
            "content": {"role": "model", "parts": [{"text": self._text}]},
        }


# --- no-op when RUN_ID (run_id) is unset --------------------------------

def test_report_started_noop_when_run_id_none(monkeypatch):
    monkeypatch.setattr(run_status, "_get_client", _fail_if_called)
    run_status.report_started(None, "techdebt", "github", "owner/repo")


def test_report_branch_ready_noop_when_run_id_none(monkeypatch):
    monkeypatch.setattr(run_status, "_get_client", _fail_if_called)
    run_status.report_branch_ready(None, "proj_agent_123", "http://sonar/dashboard?id=proj")


def test_report_finished_noop_when_run_id_none(monkeypatch):
    monkeypatch.setattr(run_status, "_get_client", _fail_if_called)
    run_status.report_finished(None, "succeeded", final_report={"branch_name": "x"})


def test_report_event_noop_when_run_id_none(monkeypatch):
    monkeypatch.setattr(run_status, "_get_client", _fail_if_called)
    run_status.report_event(None, _FakeEvent())


# --- no-op when Firestore/ADC isn't reachable ---------------------------

def test_noop_when_get_client_returns_none(monkeypatch):
    monkeypatch.setattr(run_status, "_get_client", lambda: None)
    # Should not raise even with a real run_id -- no client means no write.
    run_status.report_started("run-1", "techdebt", "github", "owner/repo")


def test_get_client_returns_none_when_firestore_package_missing(monkeypatch):
    monkeypatch.setattr(run_status, "firestore", None)
    monkeypatch.setattr(run_status, "_client", None)
    monkeypatch.setattr(run_status, "_client_init_attempted", False)
    assert run_status._get_client() is None


def test_get_client_returns_none_when_client_construction_raises(monkeypatch):
    def _raise():
        raise Exception("no ADC credentials found")

    fake_firestore = type("FakeFirestoreModule", (), {"Client": staticmethod(_raise)})
    monkeypatch.setattr(run_status, "firestore", fake_firestore)
    monkeypatch.setattr(run_status, "_client", None)
    monkeypatch.setattr(run_status, "_client_init_attempted", False)
    assert run_status._get_client() is None


def test_update_swallows_exceptions_from_set(monkeypatch):
    class _ExplodingClient:
        def collection(self, name):
            class _C:
                def document(self, doc_id):
                    class _D:
                        def set(self, fields, merge=True):
                            raise Exception("quota exceeded")
                    return _D()
            return _C()

    monkeypatch.setattr(run_status, "_get_client", lambda: _ExplodingClient())
    # Must not raise -- a Firestore hiccup can never fail the actual run.
    run_status.report_started("run-1", "techdebt", "github", "owner/repo")


# --- actual writes when a run_id and a working client are present ------

def test_report_started_writes_expected_fields(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_started("run-1", "techdebt", "github", "owner/repo", "main")

    doc = client.store["run-1"]
    assert doc["agent_type"] == "techdebt"
    assert doc["source_type"] == "github"
    assert doc["source"] == "owner/repo"
    assert doc["source_branch"] == "main"
    assert doc["status"] == "running"
    assert "started_at" in doc


def test_report_branch_ready_writes_expected_fields(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_branch_ready("run-1", "proj_agent_123", "http://sonar/dashboard?id=proj&branch=proj_agent_123")

    doc = client.store["run-1"]
    assert doc["branch_name"] == "proj_agent_123"
    assert doc["sonar_dashboard_url"] == "http://sonar/dashboard?id=proj&branch=proj_agent_123"


def test_report_finished_success_writes_final_report(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    final_report = {"branch_name": "proj_agent_123", "issues_fixed": []}
    run_status.report_finished("run-1", "succeeded", final_report=final_report)

    doc = client.store["run-1"]
    assert doc["status"] == "succeeded"
    assert doc["final_report"] == final_report
    assert "finished_at" in doc
    assert "error" not in doc


def test_report_finished_failure_writes_error_not_final_report(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_finished("run-1", "failed", error="build failed")

    doc = client.store["run-1"]
    assert doc["status"] == "failed"
    assert doc["error"] == "build failed"
    assert "final_report" not in doc


def test_updates_across_calls_merge_not_clobber(monkeypatch):
    # report_started then report_branch_ready then report_finished should
    # all land on the SAME doc, accumulating fields -- exactly what the
    # dashboard needs to show one row per run across its whole lifecycle.
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_started("run-1", "techdebt", "github", "owner/repo")
    run_status.report_branch_ready("run-1", "proj_agent_123", "http://sonar/x")
    run_status.report_finished("run-1", "succeeded", final_report={"a": 1})

    doc = client.store["run-1"]
    assert doc["agent_type"] == "techdebt"
    assert doc["branch_name"] == "proj_agent_123"
    assert doc["status"] == "succeeded"
    assert doc["final_report"] == {"a": 1}


# --- report_event ---------------------------------------------------------

def test_report_event_swallows_exceptions_from_model_dump(monkeypatch):
    class _BoomEvent:
        id = "evt-1"

        def model_dump(self, **kw):
            raise Exception("serialization exploded")

    monkeypatch.setattr(run_status, "_get_client", lambda: _FakeClient())
    # Must not raise -- a transcript-write failure can never fail the run.
    run_status.report_event("run-1", _BoomEvent())


def test_report_event_swallows_exceptions_from_firestore_write(monkeypatch):
    class _ExplodingSubcollectionClient:
        def collection(self, name):
            class _RunsCollection:
                def document(self, doc_id):
                    class _RunDoc:
                        def collection(self, name):
                            class _EventsCollection:
                                def document(self, doc_id):
                                    class _EventDoc:
                                        def set(self, fields, merge=True):
                                            raise Exception("quota exceeded")
                                    return _EventDoc()
                            return _EventsCollection()
                    return _RunDoc()
            return _RunsCollection()

    monkeypatch.setattr(run_status, "_get_client", lambda: _ExplodingSubcollectionClient())
    run_status.report_event("run-1", _FakeEvent())


def test_report_event_writes_to_events_subcollection(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_event("run-1", _FakeEvent(event_id="evt-1", author="fix_llm_agent", text="fixed Foo.java"))

    # Doesn't touch the run's own top-level fields...
    assert "run-1" not in client.store or "status" not in client.store.get("run-1", {})
    # ...lands in runs/run-1/events/evt-1 instead.
    event_doc = client.events_store("run-1")["evt-1"]
    assert event_doc["author"] == "fix_llm_agent"
    assert event_doc["content"]["parts"][0]["text"] == "fixed Foo.java"


def test_report_event_multiple_events_land_as_separate_docs(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(run_status, "_get_client", lambda: client)

    run_status.report_event("run-1", _FakeEvent(event_id="evt-1", text="first"))
    run_status.report_event("run-1", _FakeEvent(event_id="evt-2", text="second"))

    events_store = client.events_store("run-1")
    assert set(events_store.keys()) == {"evt-1", "evt-2"}
    assert events_store["evt-1"]["content"]["parts"][0]["text"] == "first"
    assert events_store["evt-2"]["content"]["parts"][0]["text"] == "second"

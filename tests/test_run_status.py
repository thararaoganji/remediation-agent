from core.tools import run_status


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def set(self, fields, merge=True):
        assert merge is True  # every write must merge, never clobber earlier fields
        self._store.setdefault(self._doc_id, {}).update(fields)


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)


class _FakeClient:
    def __init__(self):
        self.store: dict = {}

    def collection(self, name):
        assert name == "runs"
        return _FakeCollection(self.store)


def _fail_if_called(*a, **kw):
    raise AssertionError("Firestore should not have been touched")


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

"""Firestore RunStatusReporter -- identical logic to the old flat
run_status.py, wrapped in a class so it satisfies run_status.
RunStatusReporter alongside run_status_azure.CosmosRunStatusReporter.

Every public method is a no-op whenever Firestore/Application Default
Credentials aren't reachable for any reason -- a run started the existing
way (bare `python run_local.py`, or a Cloud Run Job execution without
RUN_ID) is completely unaffected; nothing here is a required dependency
for a normal agent run. Exceptions are deliberately swallowed broadly (not
narrowed to specific Firestore error types) because the actual remediation
run must never fail, slow down, or change behavior over a status-reporting
hiccup -- a missing package, no credentials, no network, a permissions
error, and a quota error are all equally "this run doesn't get to show up
on the dashboard, and that's fine."""

import datetime

try:
    from google.cloud import firestore
except ImportError:  # google-cloud-firestore isn't installed everywhere this runs
    firestore = None

from .run_status import RunStatusReporter

_COLLECTION = "runs"


class FirestoreRunStatusReporter(RunStatusReporter):
    def __init__(self):
        self._client = None
        self._client_init_attempted = False

    def _get_client(self):
        if firestore is None:
            return None
        if self._client_init_attempted:
            return self._client
        self._client_init_attempted = True
        try:
            self._client = firestore.Client()
        except Exception:
            self._client = None
        return self._client

    def _update(self, run_id: str | None, fields: dict) -> None:
        if not run_id:
            return
        client = self._get_client()
        if client is None:
            return
        try:
            client.collection(_COLLECTION).document(run_id).set(fields, merge=True)
        except Exception:
            pass

    def report_started(
        self, run_id: str | None, agent_type: str, source_type: str, source: str, source_branch: str | None = None
    ) -> None:
        self._update(run_id, {
            "agent_type": agent_type,
            "source_type": source_type,
            "source": source,
            "source_branch": source_branch,
            "status": "running",
            "started_at": datetime.datetime.now(datetime.timezone.utc),
        })

    def report_branch_ready(self, run_id: str | None, branch_name: str, sonar_dashboard_url: str) -> None:
        self._update(run_id, {
            "branch_name": branch_name,
            "sonar_dashboard_url": sonar_dashboard_url,
        })

    def report_finished(
        self, run_id: str | None, status: str, final_report: dict | None = None, error: str | None = None
    ) -> None:
        fields: dict = {
            "status": status,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
        }
        if final_report is not None:
            fields["final_report"] = final_report
        if error is not None:
            fields["error"] = error
        self._update(run_id, fields)

    def report_event(self, run_id: str | None, event) -> None:
        """Persists one full ADK Event (author, content parts -- text/thought/
        function_call/function_response --, state_delta, usage_metadata, error
        fields) to runs/{run_id}/events/{event.id} for the dashboard's live
        transcript view -- everything run_local.py already prints to stdout,
        just also kept somewhere the dashboard can read it. event.model_dump()
        handles full serialization; no field-by-field extraction needed (Event
        is a Pydantic model -- confirmed against the installed google-adk).
        Same no-op/best-effort contract as every other method here: a
        transcript-write failure must never affect the actual run."""
        if not run_id:
            return
        client = self._get_client()
        if client is None:
            return
        try:
            data = event.model_dump(mode="json", exclude_none=True)
            client.collection(_COLLECTION).document(run_id).collection("events").document(event.id).set(data, merge=True)
        except Exception:
            pass

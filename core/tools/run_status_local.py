"""MongoDB RunStatusReporter -- local-dev equivalent of run_status_gcp.py/
run_status_azure.py, backed by the `mongo` service in docker-compose.yml.
Same best-effort/silent-no-op contract as both real-cloud reporters: a
status-reporting hiccup must never break a real remediation run. Reached
via MONGO_URL, which job_runner_local.py always sets alongside CLOUD_PROVIDER
=local for every run it starts -- see that module's docstring for why the
two must travel together.

events land in their own flat `events` collection (matching
run_status_azure.py's Cosmos design, not run_status_gcp.py's Firestore
subcollections -- Mongo has no native subcollection concept either), keyed
by "{run_id}:{event.id}" rather than a bare event id so two different
runs' events can never collide even if event ids were ever reused."""

import datetime
import os

try:
    from pymongo import MongoClient
except ImportError:  # pymongo isn't installed everywhere this runs
    MongoClient = None

from .run_status import RunStatusReporter


class MongoRunStatusReporter(RunStatusReporter):
    def __init__(self):
        self._client = None
        self._client_init_attempted = False

    def _get_client(self):
        if MongoClient is None:
            return None
        if self._client_init_attempted:
            return self._client
        self._client_init_attempted = True
        try:
            self._client = MongoClient(os.environ["MONGO_URL"])
        except Exception:
            self._client = None
        return self._client

    def _db(self):
        client = self._get_client()
        if client is None:
            return None
        return client[os.environ.get("MONGO_DB", "dashboard")]

    def _update(self, run_id: str | None, fields: dict) -> None:
        if not run_id:
            return
        db = self._db()
        if db is None:
            return
        try:
            db["runs"].update_one({"_id": run_id}, {"$set": fields}, upsert=True)
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
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if final_report is not None:
            fields["final_report"] = final_report
        if error is not None:
            fields["error"] = error
        self._update(run_id, fields)

    def report_event(self, run_id: str | None, event) -> None:
        if not run_id:
            return
        db = self._db()
        if db is None:
            return
        try:
            data = event.model_dump(mode="json", exclude_none=True)
            db["events"].update_one(
                {"_id": f"{run_id}:{event.id}"},
                {"$set": {**data, "id": event.id, "run_id": run_id}},
                upsert=True,
            )
        except Exception:
            pass

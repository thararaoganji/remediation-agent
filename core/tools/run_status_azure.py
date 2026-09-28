"""Cosmos DB RunStatusReporter -- Azure equivalent of run_status_gcp.py,
same best-effort/silent-no-op contract (a status-reporting hiccup must
never break a real remediation run). Auth is keyless via
DefaultAzureCredential, matching dashboard/backend/app/storage_azure.py.

events lands in its own top-level "events" container (partition key
/run_id) rather than a Firestore-style subcollection -- Cosmos has no such
concept -- with an explicit run_id field added so
dashboard/backend/app/event_stream_azure.py's polling query
(WHERE c.run_id = @run_id) can find them."""

import datetime
import os

try:
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential
except ImportError:  # azure-cosmos/azure-identity aren't installed everywhere this runs
    CosmosClient = None

from .run_status import RunStatusReporter

_DATABASE_NAME = "dashboard"


class CosmosRunStatusReporter(RunStatusReporter):
    def __init__(self):
        self._client = None
        self._client_init_attempted = False

    def _get_client(self):
        if CosmosClient is None:
            return None
        if self._client_init_attempted:
            return self._client
        self._client_init_attempted = True
        try:
            endpoint = os.environ["AZURE_COSMOS_ENDPOINT"]
            # managed_identity_client_id: see storage_azure.py's
            # CosmosStore._get_client -- this job's USER-assigned identity
            # can't be resolved by DefaultAzureCredential without this
            # hint. Missing it here (unlike every other Azure client in
            # this codebase) meant every report_started/report_finished
            # write silently failed -- caught by _update()'s bare except,
            # by design, so a status-reporting hiccup never breaks a real
            # run -- leaving the dashboard stuck on "running" forever even
            # though the job itself succeeded.
            credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID"))
            self._client = CosmosClient(url=endpoint, credential=credential)
        except Exception:
            self._client = None
        return self._client

    def _container(self, name: str):
        client = self._get_client()
        if client is None:
            return None
        return client.get_database_client(_DATABASE_NAME).get_container_client(name)

    def _update(self, run_id: str | None, fields: dict) -> None:
        if not run_id:
            return
        container = self._container("runs")
        if container is None:
            return
        try:
            try:
                existing = container.read_item(item=run_id, partition_key=run_id)
            except Exception:
                existing = {}
            container.upsert_item(body={**existing, **fields, "id": run_id})
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
        container = self._container("events")
        if container is None:
            return
        try:
            data = event.model_dump(mode="json", exclude_none=True)
            container.upsert_item(body={**data, "id": event.id, "run_id": run_id})
        except Exception:
            pass

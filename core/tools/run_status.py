"""Optional cloud reporting of a run's live status -- for the web
dashboard to show run history/status without polling the job's own logs
(see docs/GCP_DEPLOYMENT.md and the dashboard/ directory). Same registry
pattern as dashboard/backend/app/storage.py and core/adapters/base.py's
LanguageAdapter/ADAPTER_REGISTRY: one ABC, one concrete class per cloud
provider (run_status_gcp.py / run_status_azure.py), selected by the
CLOUD_PROVIDER env var (default "gcp", so an unset env var behaves exactly
as before this was split into providers).

Every public function here is a no-op whenever RUN_ID isn't set, or
whenever the configured provider's SDK/credentials aren't reachable for any
reason -- see each concrete reporter's own docstring for why (a run started
the existing way, with no RUN_ID at all, must be completely unaffected)."""

import os
from abc import ABC, abstractmethod


class RunStatusReporter(ABC):
    @abstractmethod
    def report_started(
        self, run_id: str | None, agent_type: str, source_type: str, source: str, source_branch: str | None = None
    ) -> None: ...

    @abstractmethod
    def report_branch_ready(self, run_id: str | None, branch_name: str, sonar_dashboard_url: str) -> None: ...

    @abstractmethod
    def report_finished(
        self, run_id: str | None, status: str, final_report: dict | None = None, error: str | None = None
    ) -> None: ...

    @abstractmethod
    def report_event(self, run_id: str | None, event) -> None: ...


def _gcp():
    from .run_status_gcp import FirestoreRunStatusReporter

    return FirestoreRunStatusReporter


def _azure():
    from .run_status_azure import CosmosRunStatusReporter

    return CosmosRunStatusReporter


def _local():
    from .run_status_local import MongoRunStatusReporter

    return MongoRunStatusReporter


RUN_STATUS_REGISTRY = {
    "gcp": _gcp,
    "azure": _azure,
    "local": _local,
}

_instance: RunStatusReporter | None = None


def get_run_status_reporter() -> RunStatusReporter:
    global _instance
    if _instance is None:
        provider = os.environ.get("CLOUD_PROVIDER", "gcp")
        if provider not in RUN_STATUS_REGISTRY:
            raise ValueError(f"No RunStatusReporter registered for CLOUD_PROVIDER={provider!r}, expected one of {sorted(RUN_STATUS_REGISTRY)}")
        _instance = RUN_STATUS_REGISTRY[provider]()()
    return _instance


def report_started(
    run_id: str | None, agent_type: str, source_type: str, source: str, source_branch: str | None = None
) -> None:
    get_run_status_reporter().report_started(run_id, agent_type, source_type, source, source_branch)


def report_branch_ready(run_id: str | None, branch_name: str, sonar_dashboard_url: str) -> None:
    get_run_status_reporter().report_branch_ready(run_id, branch_name, sonar_dashboard_url)


def report_finished(run_id: str | None, status: str, final_report: dict | None = None, error: str | None = None) -> None:
    get_run_status_reporter().report_finished(run_id, status, final_report, error)


def report_event(run_id: str | None, event) -> None:
    get_run_status_reporter().report_event(run_id, event)

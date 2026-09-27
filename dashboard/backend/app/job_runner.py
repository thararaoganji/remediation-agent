"""Agent job-trigger interface + provider registry -- same pattern as
storage.py/secrets.py. Every existing call site keeps calling run_job()
exactly as it did when this module was cloud_run.py.

AGENT_JOB_NAMES stays here (not per-provider) -- both providers name their
three jobs identically (sonar-remediation-{agent_type}-job), only how a
name gets turned into a running execution differs."""

import os
from abc import ABC, abstractmethod

AGENT_JOB_NAMES = {
    "techdebt": "sonar-remediation-techdebt-job",
    "coverage": "sonar-remediation-coverage-job",
    "duplicate": "sonar-remediation-duplicate-job",
}


class JobRunner(ABC):
    @abstractmethod
    def run_job(self, agent_type: str, env: dict[str, str]) -> str:
        """Starts one execution of agent_type's job, with `env` overriding
        that job's own env vars/secrets for this execution only. Returns
        an identifier for the new execution immediately -- must NOT block
        until the run finishes."""
        ...


def _validate_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_JOB_NAMES:
        raise ValueError(f"agent_type must be one of {sorted(AGENT_JOB_NAMES)}, got {agent_type!r}")


def _gcp():
    from .job_runner_gcp import CloudRunJobRunner

    return CloudRunJobRunner


def _azure():
    from .job_runner_azure import ContainerAppsJobRunner

    return ContainerAppsJobRunner


JOB_RUNNER_REGISTRY = {
    "gcp": _gcp,
    "azure": _azure,
}

_instance: JobRunner | None = None


def get_job_runner() -> JobRunner:
    global _instance
    if _instance is None:
        provider = os.environ.get("CLOUD_PROVIDER", "gcp")
        if provider not in JOB_RUNNER_REGISTRY:
            raise ValueError(f"No JobRunner registered for CLOUD_PROVIDER={provider!r}, expected one of {sorted(JOB_RUNNER_REGISTRY)}")
        _instance = JOB_RUNNER_REGISTRY[provider]()()
    return _instance


def run_job(agent_type: str, env: dict[str, str]) -> str:
    _validate_agent_type(agent_type)
    return get_job_runner().run_job(agent_type, env)

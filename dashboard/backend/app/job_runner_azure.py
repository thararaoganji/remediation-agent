"""Container Apps Jobs JobRunner -- Azure's direct equivalent to a Cloud
Run Job (run-to-completion, on-demand-triggered container, own execution
history). Confirmed against the installed azure-mgmt-appcontainers SDK
(inspected JobsOperations.begin_start's signature and the
JobExecutionTemplate/JobExecutionContainer/EnvironmentVar model fields
directly) rather than assumed from memory, same rigor job_runner_gcp.py's
docstring already applies to google-cloud-run.

begin_start's `template` argument REPLACES the execution's whole container
definition rather than overlaying just the given fields (unlike Cloud Run's
ContainerOverride, which only touches what you pass) -- so this fetches the
job's current image/name/command/args/resources first and only swaps in
the per-run env, to avoid accidentally starting an execution with a blank
image. Passes polling=False so begin_start returns as soon as the execution
is created rather than blocking until the container finishes -- mirrors
job_runner_gcp.py's own note that Cloud Run's run_job() LRO resolves at
Execution-creation time, not completion, which is what makes both safe to
call from a request handler that must return quickly."""

import os

from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import EnvironmentVar, JobExecutionContainer, JobExecutionTemplate

from .job_runner import AGENT_JOB_NAMES, JobRunner


class ContainerAppsJobRunner(JobRunner):
    def __init__(self):
        self._client = None

    def _get_client(self) -> ContainerAppsAPIClient:
        if self._client is None:
            self._client = ContainerAppsAPIClient(
                credential=DefaultAzureCredential(),
                subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
            )
        return self._client

    def run_job(self, agent_type: str, env: dict[str, str]) -> str:
        client = self._get_client()
        resource_group = os.environ["AZURE_RESOURCE_GROUP"]
        job_name = AGENT_JOB_NAMES[agent_type]

        job = client.jobs.get(resource_group, job_name)
        current = job.properties.template.containers[0]

        override_container = JobExecutionContainer(
            image=current.image,
            name=current.name,
            command=current.command,
            args=current.args,
            resources=current.resources,
            env=[EnvironmentVar(name=k, value=v) for k, v in env.items()],
        )
        poller = client.jobs.begin_start(
            resource_group_name=resource_group,
            job_name=job_name,
            template=JobExecutionTemplate(containers=[override_container]),
            polling=False,
        )
        execution = poller.result()
        return execution.name

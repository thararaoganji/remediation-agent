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
            # managed_identity_client_id: see storage_azure.py's
            # CosmosStore._get_client -- this Container App has a
            # USER-assigned identity, which DefaultAzureCredential can't
            # resolve without an explicit client id hint.
            self._client = ContainerAppsAPIClient(
                credential=DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_MANAGED_IDENTITY_CLIENT_ID")),
                subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
            )
        return self._client

    def run_job(self, agent_type: str, env: dict[str, str]) -> str:
        client = self._get_client()
        resource_group = os.environ["AZURE_RESOURCE_GROUP"]
        job_name = AGENT_JOB_NAMES[agent_type]

        job = client.jobs.get(resource_group, job_name)
        current = job.properties.template.containers[0]

        # `env` here is only the small set of PER-RUN values runs.py's
        # create_run() resolves (RUN_ID, SONAR_BASE_URL, the LLM/GitHub
        # secrets, etc.) -- never the job's baked-in defaults
        # (CLOUD_PROVIDER, AZURE_MANAGED_IDENTITY_CLIENT_ID,
        # AZURE_COSMOS_ENDPOINT, AZURE_KEY_VAULT_URL -- see the Tofu
        # module's "Baked-in fallback values" env blocks). Since `template`
        # REPLACES the whole container rather than overlaying (this
        # docstring's own opening paragraph), building the override from
        # ONLY the per-run dict -- as an earlier version of this function
        # did -- silently wiped those baked-in vars from every real
        # execution. That's not cosmetic: with CLOUD_PROVIDER gone,
        # core/tools/run_status.py's os.environ.get("CLOUD_PROVIDER",
        # "gcp") falls back to "gcp", so every run tried (and silently
        # failed, by design) to report status to Firestore instead of
        # Cosmos -- confirmed the hard way: runs stayed stuck on
        # "running" forever with zero events, even though the job itself
        # completed successfully. Starting from current.env and
        # overlaying the per-run dict on top (per-run wins on overlapping
        # keys like SONAR_BASE_URL/LANGUAGE/CE_EDITION/AGENT_TYPE, which
        # the job's own env intentionally seeds with placeholders for
        # exactly this override) keeps both.
        merged_env = {v.name: v.value for v in (current.env or [])}
        merged_env.update(env)

        override_container = JobExecutionContainer(
            image=current.image,
            name=current.name,
            command=current.command,
            args=current.args,
            resources=current.resources,
            env=[EnvironmentVar(name=k, value=v) for k, v in merged_env.items()],
        )
        poller = client.jobs.begin_start(
            resource_group_name=resource_group,
            job_name=job_name,
            template=JobExecutionTemplate(containers=[override_container]),
            polling=False,
        )
        execution = poller.result()
        return execution.name

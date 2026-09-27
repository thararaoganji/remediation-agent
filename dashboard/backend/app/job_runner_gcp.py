"""Cloud Run Jobs JobRunner -- identical logic to the old cloud_run.py,
wrapped in a class so it satisfies job_runner.JobRunner alongside
job_runner_azure.ContainerAppsJobRunner.

Assumes each job has exactly one container (true for all three --
docs/GCP_DEPLOYMENT.md's `gcloud run jobs create` calls each specify a
single --image), so ContainerOverride doesn't need to name a specific
container -- Cloud Run applies an unnamed override to a job's sole
container."""

import os

from google.cloud import run_v2

from .job_runner import AGENT_JOB_NAMES, JobRunner


class CloudRunJobRunner(JobRunner):
    def __init__(self):
        self._client = None

    def _get_client(self) -> run_v2.JobsClient:
        if self._client is None:
            self._client = run_v2.JobsClient()
        return self._client

    def run_job(self, agent_type: str, env: dict[str, str]) -> str:
        """run_v2's run_job() is a long-running operation whose .metadata
        is already populated with the Execution at creation time (confirmed
        against the installed google-cloud-run client: JobsClient.run_job
        wraps the response with metadata_type=execution.Execution), well
        before the job completes -- that's what makes this safe to call
        from a request handler that must return quickly rather than block
        for the run's whole duration."""
        project_id = os.environ["GCP_PROJECT_ID"]
        region = os.environ["GCP_REGION"]
        job_path = f"projects/{project_id}/locations/{region}/jobs/{AGENT_JOB_NAMES[agent_type]}"

        request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()]
                    )
                ]
            ),
        )
        operation = self._get_client().run_job(request=request)
        return operation.metadata.name

"""Triggers a Cloud Run Job execution with per-run container-env
overrides -- lets one run choose among any number of saved Sonar-server/
GitHub-credential configs without redefining the job itself (see the
dashboard plan's Architecture section: the three jobs' own baked-in
--set-env-vars/--set-secrets become an unused fallback once this backend
is the only real caller).

Assumes each job has exactly one container (true for all three --
docs/GCP_DEPLOYMENT.md's `gcloud run jobs create` calls each specify a
single --image), so ContainerOverride doesn't need to name a specific
container -- Cloud Run applies an unnamed override to a job's sole
container."""

import os

from google.cloud import run_v2

_client = None

AGENT_JOB_NAMES = {
    "techdebt": "sonar-remediation-techdebt-job",
    "coverage": "sonar-remediation-coverage-job",
    "duplicate": "sonar-remediation-duplicate-job",
}


def _get_client() -> run_v2.JobsClient:
    global _client
    if _client is None:
        _client = run_v2.JobsClient()
    return _client


def run_job(agent_type: str, env: dict[str, str]) -> str:
    """Starts one execution of agent_type's Cloud Run Job, with `env`
    overriding that job's own env vars/secrets for this execution only.
    Returns the new Execution's resource name immediately -- does NOT
    wait for the run to finish. run_v2's run_job() is a long-running
    operation whose .metadata is already populated with the Execution at
    creation time (confirmed against the installed google-cloud-run
    client: JobsClient.run_job wraps the response with
    metadata_type=execution.Execution), well before the job completes --
    that's what makes this safe to call from a request handler that must
    return quickly rather than block for the run's whole duration."""
    if agent_type not in AGENT_JOB_NAMES:
        raise ValueError(f"agent_type must be one of {sorted(AGENT_JOB_NAMES)}, got {agent_type!r}")

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
    operation = _get_client().run_job(request=request)
    return operation.metadata.name

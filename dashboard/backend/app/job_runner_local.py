"""Docker-in-Docker JobRunner -- local-dev equivalent of Cloud Run Jobs/
Container Apps Jobs: starts a real sibling container from the same agent
image (the repo's root Dockerfile) for each run, on the same docker-compose
network as the rest of the stack, so it can reach `mongo` by hostname
exactly like a deployed job reaches Firestore/Cosmos DB.

Needs the host's Docker socket mounted into the backend container
(docker-compose.yml's `volumes: - /var/run/docker.sock:/var/run/docker.sock`)
-- this is "Docker outside of Docker", not Docker-in-Docker proper: the
backend talks to the HOST's real Docker daemon and asks it to start a
container alongside itself, rather than running a nested daemon.

Non-blocking by the same contract every other JobRunner has: `run()` with
detach=True starts the container and returns immediately, exactly like
Cloud Run's run_job() LRO and Container Apps' begin_start(polling=False)
both do -- the caller (routers/runs.py) must never wait for a run to
actually finish."""

import os

import docker

from .job_runner import AGENT_JOB_NAMES, JobRunner


class LocalDockerJobRunner(JobRunner):
    def __init__(self):
        self._client = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def run_job(self, agent_type: str, env: dict[str, str]) -> str:
        # `env` here is only the per-run values runs.py's create_run()
        # resolves (RUN_ID, SONAR_BASE_URL, secrets, ...) -- never this
        # setup's own baked-in defaults (CLOUD_PROVIDER, MONGO_URL). Start
        # from those defaults and let the per-run dict win on any
        # overlapping key -- the exact env-merge lesson job_runner_azure.py
        # learned the hard way (see its own docstring): a full REPLACE
        # here would silently strip CLOUD_PROVIDER, and the container
        # would default to "gcp" and try to reach a real Google Cloud
        # project that doesn't exist in a local dev environment.
        baked_in_defaults = {
            "CLOUD_PROVIDER": "local",
            "MONGO_URL": os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
            "MONGO_DB": os.environ.get("MONGO_DB", "dashboard"),
        }
        merged_env = {**baked_in_defaults, **env}

        image = os.environ.get("AGENT_IMAGE", "sonar-remediation-agent-local:latest")
        network = os.environ.get("DOCKER_NETWORK", "sonar-remediation-local")

        container = self._get_client().containers.run(
            image,
            environment=merged_env,
            network=network,
            detach=True,
            auto_remove=True,
            name=f"{AGENT_JOB_NAMES[agent_type]}-{merged_env['RUN_ID'][:8]}",
        )
        return container.id

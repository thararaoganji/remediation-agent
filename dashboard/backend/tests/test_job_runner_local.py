"""Tests for LocalDockerJobRunner -- same "env must be merged, not
replaced" concern job_runner_azure.py's own tests cover, applied here to
make sure CLOUD_PROVIDER/MONGO_URL/MONGO_DB always survive into the
container even though runs.py's create_run() never sets them itself."""

from app.job_runner_local import LocalDockerJobRunner


class _FakeContainer:
    def __init__(self, container_id):
        self.id = container_id


class _FakeContainers:
    def __init__(self):
        self.calls = []

    def run(self, image, environment, network, detach, auto_remove, name):
        self.calls.append({
            "image": image, "environment": environment, "network": network,
            "detach": detach, "auto_remove": auto_remove, "name": name,
        })
        return _FakeContainer("fake-container-id")


class _FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainers()


def _runner_with_fake_docker():
    runner = LocalDockerJobRunner()
    fake_client = _FakeDockerClient()
    runner._get_client = lambda: fake_client
    return runner, fake_client


def test_run_job_includes_baked_in_local_defaults(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    monkeypatch.setenv("MONGO_DB", "dashboard")
    runner, client = _runner_with_fake_docker()

    runner.run_job("techdebt", {"RUN_ID": "run-12345678", "SONAR_BASE_URL": "https://sonar.example.com"})

    env = client.containers.calls[0]["environment"]
    assert env["CLOUD_PROVIDER"] == "local"
    assert env["MONGO_URL"] == "mongodb://mongo:27017"
    assert env["MONGO_DB"] == "dashboard"
    assert env["RUN_ID"] == "run-12345678"
    assert env["SONAR_BASE_URL"] == "https://sonar.example.com"


def test_run_job_per_run_env_does_not_lose_cloud_provider(monkeypatch):
    # The exact bug class job_runner_azure.py hit in production: if the
    # per-run dict were used to fully REPLACE the container's env instead
    # of merging on top of the local defaults, CLOUD_PROVIDER would vanish
    # and the container would silently default to "gcp" inside a local
    # dev environment with no GCP project at all.
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    runner, client = _runner_with_fake_docker()

    runner.run_job("techdebt", {"RUN_ID": "run-1", "GITHUB_TOKEN": "ghp_abc"})

    env = client.containers.calls[0]["environment"]
    assert env["CLOUD_PROVIDER"] == "local"
    assert env["GITHUB_TOKEN"] == "ghp_abc"


def test_run_job_uses_agent_image_and_network_env_vars(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    monkeypatch.setenv("AGENT_IMAGE", "custom-agent-image:latest")
    monkeypatch.setenv("DOCKER_NETWORK", "custom-network")
    runner, client = _runner_with_fake_docker()

    runner.run_job("coverage", {"RUN_ID": "run-1"})

    call = client.containers.calls[0]
    assert call["image"] == "custom-agent-image:latest"
    assert call["network"] == "custom-network"
    assert call["detach"] is True
    assert call["auto_remove"] is True


def test_run_job_container_name_includes_agent_job_name(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    runner, client = _runner_with_fake_docker()

    runner.run_job("duplicate", {"RUN_ID": "run-abcdefgh-1234"})

    assert client.containers.calls[0]["name"].startswith("sonar-remediation-duplicate-job-")


def test_run_job_returns_container_id(monkeypatch):
    monkeypatch.setenv("MONGO_URL", "mongodb://mongo:27017")
    runner, _ = _runner_with_fake_docker()

    result = runner.run_job("techdebt", {"RUN_ID": "run-1"})

    assert result == "fake-container-id"

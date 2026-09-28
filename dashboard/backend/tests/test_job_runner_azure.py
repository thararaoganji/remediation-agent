"""Regression test for a real production bug: run_job() built the
execution's env entirely from the per-run overrides passed in by
runs.py's create_run(), dropping every baked-in env var the job's own
Tofu definition sets (CLOUD_PROVIDER, AZURE_MANAGED_IDENTITY_CLIENT_ID,
AZURE_COSMOS_ENDPOINT, AZURE_KEY_VAULT_URL). Since begin_start's
`template` REPLACES the whole container rather than overlaying (unlike
Cloud Run's ContainerOverride), every real run silently lost
CLOUD_PROVIDER -- core/tools/run_status.py then defaulted to "gcp" and
tried (and silently failed) to report status to Firestore instead of
Cosmos. Confirmed against real Azure infra: jobs completed successfully
but the dashboard never saw a single status update or event."""

from types import SimpleNamespace

from app.job_runner_azure import ContainerAppsJobRunner


class _FakeEnvVar:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeJobsOperations:
    def __init__(self, current_env):
        self._current_env = current_env
        self.captured_template = None

    def get(self, resource_group, job_name):
        container = SimpleNamespace(
            image="sonarremediationsandboxacr.azurecr.io/sonar-remediation-agent:latest",
            name="agent",
            command=None,
            args=None,
            resources=SimpleNamespace(cpu=2.0, memory="4Gi"),
            env=[_FakeEnvVar(k, v) for k, v in self._current_env.items()],
        )
        return SimpleNamespace(properties=SimpleNamespace(template=SimpleNamespace(containers=[container])))

    def begin_start(self, resource_group_name, job_name, template, polling):
        self.captured_template = template

        class _Poller:
            def result(self_inner):
                return SimpleNamespace(name="sonar-remediation-techdebt-job-abc123")

        return _Poller()


def _runner_with_baked_env(baked_env: dict, monkeypatch):
    runner = ContainerAppsJobRunner()
    fake_jobs = _FakeJobsOperations(baked_env)
    monkeypatch.setattr(runner, "_get_client", lambda: SimpleNamespace(jobs=fake_jobs))
    return runner, fake_jobs


def test_run_job_preserves_baked_in_env_vars(monkeypatch):
    baked_env = {
        "CLOUD_PROVIDER": "azure",
        "AZURE_MANAGED_IDENTITY_CLIENT_ID": "identity-client-id",
        "AZURE_COSMOS_ENDPOINT": "https://example.documents.azure.com:443/",
        "AZURE_KEY_VAULT_URL": "https://example.vault.azure.net/",
        "SONAR_BASE_URL": "http://placeholder:9000",
    }
    runner, fake_jobs = _runner_with_baked_env(baked_env, monkeypatch)
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "sonar-remediation-sandbox-rg")

    runner.run_job("techdebt", {"RUN_ID": "run-1", "SONAR_BASE_URL": "https://real-sonar.example.com"})

    result_env = {v.name: v.value for v in fake_jobs.captured_template.containers[0].env}
    assert result_env["CLOUD_PROVIDER"] == "azure"
    assert result_env["AZURE_MANAGED_IDENTITY_CLIENT_ID"] == "identity-client-id"
    assert result_env["AZURE_COSMOS_ENDPOINT"] == "https://example.documents.azure.com:443/"
    assert result_env["AZURE_KEY_VAULT_URL"] == "https://example.vault.azure.net/"


def test_run_job_per_run_env_overrides_baked_in_placeholder(monkeypatch):
    baked_env = {"SONAR_BASE_URL": "http://placeholder:9000", "CLOUD_PROVIDER": "azure"}
    runner, fake_jobs = _runner_with_baked_env(baked_env, monkeypatch)
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "sonar-remediation-sandbox-rg")

    runner.run_job("techdebt", {"RUN_ID": "run-1", "SONAR_BASE_URL": "https://real-sonar.example.com"})

    result_env = {v.name: v.value for v in fake_jobs.captured_template.containers[0].env}
    assert result_env["SONAR_BASE_URL"] == "https://real-sonar.example.com"


def test_run_job_adds_new_per_run_keys_not_present_in_baked_env(monkeypatch):
    baked_env = {"CLOUD_PROVIDER": "azure"}
    runner, fake_jobs = _runner_with_baked_env(baked_env, monkeypatch)
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "sonar-remediation-sandbox-rg")

    runner.run_job("techdebt", {"RUN_ID": "run-1", "GITHUB_TOKEN": "ghp_abc"})

    result_env = {v.name: v.value for v in fake_jobs.captured_template.containers[0].env}
    assert result_env["RUN_ID"] == "run-1"
    assert result_env["GITHUB_TOKEN"] == "ghp_abc"
    assert result_env["CLOUD_PROVIDER"] == "azure"


def test_run_job_preserves_image_and_resources(monkeypatch):
    runner, fake_jobs = _runner_with_baked_env({}, monkeypatch)
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "sonar-remediation-sandbox-rg")

    runner.run_job("techdebt", {"RUN_ID": "run-1"})

    container = fake_jobs.captured_template.containers[0]
    assert container.image == "sonarremediationsandboxacr.azurecr.io/sonar-remediation-agent:latest"
    assert container.resources.cpu == 2.0

import pytest

from app import cloud_run


def _make_sonar_server(client, name="Prod Sonar", ce_edition=True, token="sonar-tok"):
    return client.post("/api/sonar-servers", json={
        "name": name, "base_url": f"http://{name.lower().replace(' ', '-')}:9000",
        "ce_edition": ce_edition, "token": token,
    }).json()


def _make_github_credential(client, name="Acme Org", token="gh-tok"):
    return client.post("/api/github-credentials", json={"name": name, "token": token}).json()


def test_create_run_github_source_starts_job_with_resolved_secrets(client, fake_cloud_run):
    sonar = _make_sonar_server(client)
    github = _make_github_credential(client)

    resp = client.post("/api/runs", json={
        "agent_type": "techdebt",
        "source_type": "github",
        "source": "owner/repo",
        "source_branch": "develop",
        "language": "java",
        "sonar_server_id": sonar["id"],
        "github_credential_id": github["id"],
    })
    assert resp.status_code == 201
    run = resp.json()
    assert run["status"] == "running"
    assert run["execution_name"].endswith("/executions/fake-execution-1")

    assert len(fake_cloud_run.calls) == 1
    job_path, env = fake_cloud_run.calls[0]
    assert job_path.endswith(cloud_run.AGENT_JOB_NAMES["techdebt"])
    assert env["RUN_ID"] == run["id"]
    assert env["SOURCE_TYPE"] == "github"
    assert env["GITHUB_REPO"] == "owner/repo"
    assert env["SOURCE_BRANCH"] == "develop"
    assert env["SONAR_BASE_URL"] == sonar["base_url"]
    assert env["CE_EDITION"] == "true"
    assert env["SONAR_TOKEN"] == "sonar-tok"  # resolved from Secret Manager, not the id
    assert env["GITHUB_TOKEN"] == "gh-tok"


def test_create_run_local_source_omits_github_env(client, fake_cloud_run):
    sonar = _make_sonar_server(client)

    resp = client.post("/api/runs", json={
        "agent_type": "coverage",
        "source_type": "local",
        "source": "/repos/my-project",
        "sonar_server_id": sonar["id"],
    })
    assert resp.status_code == 201
    _, env = fake_cloud_run.calls[0]
    assert env["SOURCE_PATH"] == "/repos/my-project"
    assert "GITHUB_REPO" not in env
    assert "GITHUB_TOKEN" not in env


def test_create_run_picks_correct_job_per_agent_type(client, fake_cloud_run):
    sonar = _make_sonar_server(client)
    for agent_type, job_name in cloud_run.AGENT_JOB_NAMES.items():
        client.post("/api/runs", json={
            "agent_type": agent_type, "source_type": "local", "source": "/x", "sonar_server_id": sonar["id"],
        })
    job_paths = [call[0] for call in fake_cloud_run.calls]
    assert job_paths == [f"projects/test-project/locations/us-central1/jobs/{name}" for name in cloud_run.AGENT_JOB_NAMES.values()]


def test_create_run_unknown_sonar_server_400s_without_starting_job(client, fake_cloud_run):
    resp = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "local", "source": "/x", "sonar_server_id": "nope",
    })
    assert resp.status_code == 400
    assert fake_cloud_run.calls == []


def test_create_run_unknown_github_credential_400s_without_starting_job(client, fake_cloud_run):
    sonar = _make_sonar_server(client)
    resp = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "github", "source": "owner/repo",
        "sonar_server_id": sonar["id"], "github_credential_id": "nope",
    })
    assert resp.status_code == 400
    assert fake_cloud_run.calls == []


def test_create_run_cloud_run_failure_marks_run_failed(client, monkeypatch):
    sonar = _make_sonar_server(client)

    class _FailingCloudRunClient:
        def run_job(self, request):
            raise Exception("simulated Cloud Run API error")

    monkeypatch.setattr(cloud_run, "_get_client", lambda: _FailingCloudRunClient())

    resp = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "local", "source": "/x", "sonar_server_id": sonar["id"],
    })
    assert resp.status_code == 502

    runs = client.get("/api/runs").json()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "simulated Cloud Run API error" in runs[0]["error"]


def test_list_runs_newest_first(client, fake_cloud_run):
    sonar = _make_sonar_server(client)
    first = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "local", "source": "/a", "sonar_server_id": sonar["id"],
    }).json()
    second = client.post("/api/runs", json={
        "agent_type": "coverage", "source_type": "local", "source": "/b", "sonar_server_id": sonar["id"],
    }).json()

    listed = client.get("/api/runs").json()
    assert [r["id"] for r in listed] == [second["id"], first["id"]]


def test_get_run_detail_and_404(client, fake_cloud_run):
    sonar = _make_sonar_server(client)
    created = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "local", "source": "/a", "sonar_server_id": sonar["id"],
    }).json()

    resp = client.get(f"/api/runs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]

    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_stream_endpoint_404_for_unknown_run(client):
    assert client.get("/api/runs/does-not-exist/stream").status_code == 404


def test_stream_endpoint_streams_events_for_existing_run(client, fake_firestore):
    fake_firestore.collection("runs").document("run-1").set({"status": "succeeded", "agent_type": "techdebt"})
    fake_firestore.collection("runs").document("run-1").collection("events").document("evt-1").set(
        {"author": "fix_llm_agent", "content": {"parts": [{"text": "hello"}]}}
    )

    with client.stream("GET", "/api/runs/run-1/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())

    assert "evt-1" in body
    assert "hello" in body
    assert 'event: done\ndata: {"status": "succeeded"}' in body

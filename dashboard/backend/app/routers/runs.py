"""Triggering and tracking agent runs -- the core of the dashboard.

Doesn't touch the three Cloud Run Jobs' own definitions at all: every
value they'd otherwise get from --set-env-vars/--set-secrets is supplied
here instead, per execution, as a container override (see
core.cloud_run.run_job's docstring). That's what makes "pick any of N
saved Sonar servers / GitHub credentials per run" possible without
redefining a job for every combination.

Status lifecycle: "queued" (Firestore doc written) -> "running" (Cloud
Run accepted the execution) -> "succeeded"/"failed" (written by the agent
container itself, via core/tools/run_status.py, once it actually runs --
see run_local.py). A run that fails to even START (bad sonar_server_id,
Cloud Run API error) is marked "failed" here directly, since no container
will ever run to report anything for it."""

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import cloud_run, firestore_db, secret_manager

router = APIRouter(prefix="/api/runs", tags=["runs"])

_COLLECTION = "runs"


class RunCreate(BaseModel):
    agent_type: str  # "techdebt" | "coverage" | "duplicate"
    source_type: str  # "github" | "local" -- the dashboard's New Run form should only ever offer "github" (a "local" path only makes sense for a Job whose image already has the source baked in, not a real dashboard use case)
    source: str  # "owner/repo" for github, an absolute path for local
    source_branch: str | None = None
    language: str = "java"
    sonar_server_id: str
    github_credential_id: str | None = None  # required in practice for source_type="github"; optional so a local/no-push run isn't forced to pick one


class RunOut(BaseModel):
    id: str
    agent_type: str
    source_type: str
    source: str
    source_branch: str | None = None
    language: str
    sonar_server_id: str
    github_credential_id: str | None = None
    status: str
    branch_name: str | None = None
    sonar_dashboard_url: str | None = None
    final_report: dict[str, Any] | None = None
    error: str | None = None
    execution_name: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _to_out(doc: dict) -> RunOut:
    return RunOut(**{field: doc.get(field) for field in RunOut.model_fields})


@router.get("", response_model=list[RunOut])
def list_runs():
    return [_to_out(d) for d in firestore_db.list_docs(_COLLECTION, order_by="created_at", descending=True)]


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str):
    doc = firestore_db.get_doc(_COLLECTION, run_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _to_out(doc)


@router.post("", response_model=RunOut, status_code=201)
def create_run(body: RunCreate):
    sonar_server = firestore_db.get_doc("sonar_servers", body.sonar_server_id)
    if sonar_server is None:
        raise HTTPException(status_code=400, detail="Unknown sonar_server_id")

    github_cred = None
    if body.github_credential_id:
        github_cred = firestore_db.get_doc("github_credentials", body.github_credential_id)
        if github_cred is None:
            raise HTTPException(status_code=400, detail="Unknown github_credential_id")

    run_id = str(uuid.uuid4())
    env = {
        "RUN_ID": run_id,
        "AGENT_TYPE": body.agent_type,
        "SOURCE_TYPE": body.source_type,
        "LANGUAGE": body.language,
        "SONAR_BASE_URL": sonar_server["base_url"],
        "CE_EDITION": "true" if sonar_server["ce_edition"] else "false",
        "SONAR_TOKEN": secret_manager.access_secret_value(sonar_server["secret_name"]),
    }
    if body.source_type == "github":
        env["GITHUB_REPO"] = body.source
        if github_cred:
            env["GITHUB_TOKEN"] = secret_manager.access_secret_value(github_cred["secret_name"])
    else:
        env["SOURCE_PATH"] = body.source
    if body.source_branch:
        env["SOURCE_BRANCH"] = body.source_branch

    firestore_db.create_doc(_COLLECTION, {
        "agent_type": body.agent_type,
        "source_type": body.source_type,
        "source": body.source,
        "source_branch": body.source_branch,
        "language": body.language,
        "sonar_server_id": body.sonar_server_id,
        "github_credential_id": body.github_credential_id,
        "status": "queued",
        "created_at": datetime.now(timezone.utc),
    }, doc_id=run_id)

    try:
        execution_name = cloud_run.run_job(body.agent_type, env)
    except Exception as e:
        firestore_db.update_doc(_COLLECTION, run_id, {"status": "failed", "error": f"failed to start: {e}"})
        raise HTTPException(status_code=502, detail=f"Failed to start Cloud Run Job: {e}") from e

    firestore_db.update_doc(_COLLECTION, run_id, {"status": "running", "execution_name": execution_name})
    return _to_out(firestore_db.get_doc(_COLLECTION, run_id))

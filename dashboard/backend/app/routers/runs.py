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

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import auth, cloud_run, event_stream, firestore_db, secret_manager
from .llm_configs import VENDOR_ENV_VAR

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
    owner_email: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _to_out(doc: dict) -> RunOut:
    return RunOut(**{field: doc.get(field) for field in RunOut.model_fields})


def _owned(doc: dict, user: auth.CurrentUser) -> bool:
    return user.role == "admin" or doc.get("owner_email") == user.email


@router.get("", response_model=list[RunOut])
def list_runs(user: auth.CurrentUser = Depends(auth.get_current_user)):
    docs = firestore_db.list_docs(_COLLECTION, order_by="created_at", descending=True)
    if user.role != "admin":
        docs = [d for d in docs if d.get("owner_email") == user.email]
    return [_to_out(d) for d in docs]


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str, user: auth.CurrentUser = Depends(auth.get_current_user)):
    doc = firestore_db.get_doc(_COLLECTION, run_id)
    if doc is None or not _owned(doc, user):
        raise HTTPException(status_code=404, detail="Run not found")
    return _to_out(doc)


@router.get("/{run_id}/stream")
def stream_run(run_id: str, user: auth.CurrentUser = Depends(auth.get_current_user)):
    """Live event transcript (adk-web-style) for one run, via SSE -- see
    event_stream.stream_run()'s own docstring for the Firestore real-time
    listener bridge this wraps. Works the same whether the run is still
    going (events arrive live, connection closes once it reaches a
    terminal status) or already finished (the full history arrives
    immediately, then the connection closes right away)."""
    doc = firestore_db.get_doc(_COLLECTION, run_id)
    if doc is None or not _owned(doc, user):
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(event_stream.stream_run(run_id), media_type="text/event-stream")


@router.post("", response_model=RunOut, status_code=201)
def create_run(body: RunCreate, user: auth.CurrentUser = Depends(auth.get_current_user)):
    # Same "unknown id" message whether the id truly doesn't exist or just
    # isn't the caller's -- doesn't confirm to the caller that some other
    # user's server/credential id is real.
    sonar_server = firestore_db.get_doc("sonar_servers", body.sonar_server_id)
    if sonar_server is None or not _owned(sonar_server, user):
        raise HTTPException(status_code=400, detail="Unknown sonar_server_id")

    github_cred = None
    if body.github_credential_id:
        github_cred = firestore_db.get_doc("github_credentials", body.github_credential_id)
        if github_cred is None or not _owned(github_cred, user):
            raise HTTPException(status_code=400, detail="Unknown github_credential_id")

    # The active LLM config is global (admin-managed), not per-run-selected
    # like the Sonar server/GitHub credential above -- see
    # llm_configs.py's docstring. Failing fast here with a clear message
    # avoids a repeat of a run silently burning through every file with
    # "no fix was generated" because no LLM credential was ever resolved.
    llm_config = next((c for c in firestore_db.list_docs("llm_configs") if c.get("is_active")), None)
    if llm_config is None:
        raise HTTPException(status_code=400, detail="No active LLM API key configured -- set one up on the Connections page")

    run_id = str(uuid.uuid4())
    env = {
        "RUN_ID": run_id,
        "AGENT_TYPE": body.agent_type,
        "SOURCE_TYPE": body.source_type,
        "LANGUAGE": body.language,
        "SONAR_BASE_URL": sonar_server["base_url"],
        "CE_EDITION": "true" if sonar_server["ce_edition"] else "false",
        "SONAR_TOKEN": secret_manager.access_secret_value(sonar_server["secret_name"]),
        "LLM_VENDOR": llm_config["vendor"],
        "LLM_MODEL": llm_config["model"],
        VENDOR_ENV_VAR[llm_config["vendor"]]: secret_manager.access_secret_value(llm_config["secret_name"]),
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
        "owner_email": user.email,
        "created_at": datetime.now(timezone.utc),
    }, doc_id=run_id)

    try:
        execution_name = cloud_run.run_job(body.agent_type, env)
    except Exception as e:
        firestore_db.update_doc(_COLLECTION, run_id, {"status": "failed", "error": f"failed to start: {e}"})
        raise HTTPException(status_code=502, detail=f"Failed to start Cloud Run Job: {e}") from e

    firestore_db.update_doc(_COLLECTION, run_id, {"status": "running", "execution_name": execution_name})
    return _to_out(firestore_db.get_doc(_COLLECTION, run_id))

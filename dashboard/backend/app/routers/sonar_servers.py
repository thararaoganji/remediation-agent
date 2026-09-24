"""CRUD for named SonarQube server configs -- each gets its own Secret
Manager secret for its token (sonar-token-{id}), so the dashboard can
support multiple Sonar servers (different teams/orgs) instead of one
global URL+token, per the dashboard plan's Architecture section.

Owner-scoped: a plain "user" only ever sees/manages their own servers; an
"admin" sees/manages everyone's (confirmed requirement -- admins get full
visibility into every user's connections, not just their own runs)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, firestore_db, secret_manager
from ..validation import HttpUrlStr, NonEmptyStr

router = APIRouter(prefix="/api/sonar-servers", tags=["sonar-servers"])

_COLLECTION = "sonar_servers"


def _secret_id(server_id: str) -> str:
    return f"sonar-token-{server_id}"


class SonarServerCreate(BaseModel):
    name: NonEmptyStr
    base_url: HttpUrlStr
    ce_edition: bool = True
    token: NonEmptyStr


class SonarServerUpdate(BaseModel):
    name: NonEmptyStr | None = None
    base_url: HttpUrlStr | None = None
    ce_edition: bool | None = None
    token: NonEmptyStr | None = None  # present -> rotates the token (adds a new secret version)


class SonarServerOut(BaseModel):
    id: str
    name: str
    base_url: str
    ce_edition: bool
    owner_email: str


def _to_out(doc: dict) -> SonarServerOut:
    return SonarServerOut(
        id=doc["id"], name=doc["name"], base_url=doc["base_url"],
        ce_edition=doc["ce_edition"], owner_email=doc.get("owner_email", ""),
    )


def _get_owned_or_404(server_id: str, user: auth.CurrentUser) -> dict:
    doc = firestore_db.get_doc(_COLLECTION, server_id)
    # 404, not 403, for a resource that exists but isn't yours -- doesn't
    # confirm to the caller that some other user's server id is real.
    if doc is None or (user.role != "admin" and doc.get("owner_email") != user.email):
        raise HTTPException(status_code=404, detail="Sonar server not found")
    return doc


@router.get("", response_model=list[SonarServerOut])
def list_sonar_servers(user: auth.CurrentUser = Depends(auth.get_current_user)):
    docs = firestore_db.list_docs(_COLLECTION, order_by="name")
    if user.role != "admin":
        docs = [d for d in docs if d.get("owner_email") == user.email]
    return [_to_out(d) for d in docs]


@router.post("", response_model=SonarServerOut, status_code=201)
def create_sonar_server(body: SonarServerCreate, user: auth.CurrentUser = Depends(auth.get_current_user)):
    # secret created before the Firestore doc, using a pre-generated id, so
    # secret_name is known up front rather than needing a create-then-patch
    # dance once Firestore hands back an auto-id. Known, accepted tradeoff
    # for this internal tool: if the Firestore write below ever failed
    # after this succeeded, the secret would be orphaned (no rollback) --
    # not worth a two-phase-commit for an admin tool at this scale.
    server_id = str(uuid.uuid4())
    secret_id = _secret_id(server_id)
    secret_manager.create_secret_with_value(secret_id, body.token)
    firestore_db.create_doc(_COLLECTION, {
        "name": body.name,
        "base_url": body.base_url,
        "ce_edition": body.ce_edition,
        "secret_name": secret_id,
        "owner_email": user.email,
    }, doc_id=server_id)
    return _to_out(firestore_db.get_doc(_COLLECTION, server_id))


@router.put("/{server_id}", response_model=SonarServerOut)
def update_sonar_server(
    server_id: str, body: SonarServerUpdate, user: auth.CurrentUser = Depends(auth.get_current_user)
):
    doc = _get_owned_or_404(server_id, user)
    updates = {k: v for k, v in {
        "name": body.name, "base_url": body.base_url, "ce_edition": body.ce_edition,
    }.items() if v is not None}
    if updates:
        firestore_db.update_doc(_COLLECTION, server_id, updates)
    if body.token is not None:
        secret_manager.add_secret_version(doc["secret_name"], body.token)
    return _to_out(firestore_db.get_doc(_COLLECTION, server_id))


@router.delete("/{server_id}", status_code=204)
def delete_sonar_server(server_id: str, user: auth.CurrentUser = Depends(auth.get_current_user)):
    doc = _get_owned_or_404(server_id, user)
    secret_manager.delete_secret(doc["secret_name"])
    firestore_db.delete_doc(_COLLECTION, server_id)

"""CRUD for named GitHub credential configs -- each gets its own Secret
Manager secret for its token (github-token-{id}), mirroring
sonar_servers.py's shape. api_base_url defaults to the public GitHub API
but is overridable per credential for a GitHub Enterprise Server
instance, which has its own API base URL distinct from api.github.com."""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import firestore_db, secret_manager

router = APIRouter(prefix="/api/github-credentials", tags=["github-credentials"])

_COLLECTION = "github_credentials"
_DEFAULT_API_BASE_URL = "https://api.github.com"


def _secret_id(cred_id: str) -> str:
    return f"github-token-{cred_id}"


class GithubCredentialCreate(BaseModel):
    name: str
    api_base_url: str = _DEFAULT_API_BASE_URL
    token: str


class GithubCredentialUpdate(BaseModel):
    name: str | None = None
    api_base_url: str | None = None
    token: str | None = None  # present -> rotates the token (adds a new secret version)


class GithubCredentialOut(BaseModel):
    id: str
    name: str
    api_base_url: str


def _to_out(doc: dict) -> GithubCredentialOut:
    return GithubCredentialOut(id=doc["id"], name=doc["name"], api_base_url=doc["api_base_url"])


def _get_or_404(cred_id: str) -> dict:
    doc = firestore_db.get_doc(_COLLECTION, cred_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="GitHub credential not found")
    return doc


@router.get("", response_model=list[GithubCredentialOut])
def list_github_credentials():
    return [_to_out(d) for d in firestore_db.list_docs(_COLLECTION, order_by="name")]


@router.post("", response_model=GithubCredentialOut, status_code=201)
def create_github_credential(body: GithubCredentialCreate):
    # Same secret-before-doc tradeoff as sonar_servers.create_sonar_server --
    # see that function's comment for why.
    cred_id = str(uuid.uuid4())
    secret_id = _secret_id(cred_id)
    secret_manager.create_secret_with_value(secret_id, body.token)
    firestore_db.create_doc(_COLLECTION, {
        "name": body.name,
        "api_base_url": body.api_base_url,
        "secret_name": secret_id,
    }, doc_id=cred_id)
    return _to_out(firestore_db.get_doc(_COLLECTION, cred_id))


@router.put("/{cred_id}", response_model=GithubCredentialOut)
def update_github_credential(cred_id: str, body: GithubCredentialUpdate):
    doc = _get_or_404(cred_id)
    updates = {k: v for k, v in {
        "name": body.name, "api_base_url": body.api_base_url,
    }.items() if v is not None}
    if updates:
        firestore_db.update_doc(_COLLECTION, cred_id, updates)
    if body.token is not None:
        secret_manager.add_secret_version(doc["secret_name"], body.token)
    return _to_out(firestore_db.get_doc(_COLLECTION, cred_id))


@router.delete("/{cred_id}", status_code=204)
def delete_github_credential(cred_id: str):
    doc = _get_or_404(cred_id)
    secret_manager.delete_secret(doc["secret_name"])
    firestore_db.delete_doc(_COLLECTION, cred_id)

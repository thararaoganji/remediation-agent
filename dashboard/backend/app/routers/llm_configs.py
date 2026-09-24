"""Admin-only CRUD for LLM provider configs -- replaces the old single
global "Google API key" secret with a real list: each config names a
vendor + model + its own API key, and exactly one is "active" at a time.
The active config's vendor/model/secret are what runs.py.create_run
resolves and passes to the Cloud Run Job as env overrides (LLM_VENDOR,
LLM_MODEL, and whichever env var that vendor's key belongs in -- see
core/llm_config.py on the agent side)."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, firestore_db, secret_manager
from ..validation import NonEmptyStr

router = APIRouter(prefix="/api/llm-configs", tags=["llm-configs"], dependencies=[Depends(auth.require_admin)])

_COLLECTION = "llm_configs"

# Which env var each vendor's key needs to land in as a Cloud Run
# container override -- see core/llm_config.py on the agent side, which
# this mirrors (the dashboard backend doesn't depend on core/, so this is
# a small intentional duplication rather than a cross-package import).
VENDOR_ENV_VAR = {
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

# Kept in sync with VENDOR_ENV_VAR's keys by hand -- Literal needs its
# members spelled out at class-definition time, so this can't be derived
# from that dict directly. An unrecognized vendor is now rejected by
# Pydantic itself (422) before create/update's handler code ever runs, so
# there's no separate runtime "is this a known vendor" check left to keep
# in sync too.
Vendor = Literal["google", "openai", "anthropic"]


def _secret_id(config_id: str) -> str:
    return f"llm-api-key-{config_id}"


class LlmConfigCreate(BaseModel):
    vendor: Vendor
    model: NonEmptyStr
    api_key: NonEmptyStr


class LlmConfigUpdate(BaseModel):
    vendor: Vendor | None = None
    model: NonEmptyStr | None = None
    api_key: NonEmptyStr | None = None  # present -> rotates the token (adds a new secret version)


class LlmConfigOut(BaseModel):
    id: str
    vendor: str
    model: str
    is_active: bool


def _to_out(doc: dict) -> LlmConfigOut:
    return LlmConfigOut(id=doc["id"], vendor=doc["vendor"], model=doc["model"], is_active=doc.get("is_active", False))


def _get_or_404(config_id: str) -> dict:
    doc = firestore_db.get_doc(_COLLECTION, config_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="LLM config not found")
    return doc


@router.get("", response_model=list[LlmConfigOut])
def list_llm_configs():
    return [_to_out(d) for d in firestore_db.list_docs(_COLLECTION, order_by="vendor")]


@router.post("", response_model=LlmConfigOut, status_code=201)
def create_llm_config(body: LlmConfigCreate):
    # The very first config ever created becomes active automatically --
    # otherwise there'd be no active config at all until an admin remembers
    # to flip one on, and runs.py.create_run treats "none active" as a hard
    # failure (better to default to a sensible state than to a broken one).
    is_first = len(firestore_db.list_docs(_COLLECTION)) == 0

    config_id = str(uuid.uuid4())
    secret_id = _secret_id(config_id)
    secret_manager.create_secret_with_value(secret_id, body.api_key)
    firestore_db.create_doc(_COLLECTION, {
        "vendor": body.vendor,
        "model": body.model,
        "secret_name": secret_id,
        "is_active": is_first,
    }, doc_id=config_id)
    return _to_out(firestore_db.get_doc(_COLLECTION, config_id))


@router.put("/{config_id}", response_model=LlmConfigOut)
def update_llm_config(config_id: str, body: LlmConfigUpdate):
    doc = _get_or_404(config_id)
    updates = {k: v for k, v in {"vendor": body.vendor, "model": body.model}.items() if v is not None}
    if updates:
        firestore_db.update_doc(_COLLECTION, config_id, updates)
    if body.api_key is not None:
        secret_manager.add_secret_version(doc["secret_name"], body.api_key)
    return _to_out(firestore_db.get_doc(_COLLECTION, config_id))


@router.delete("/{config_id}", status_code=204)
def delete_llm_config(config_id: str):
    doc = _get_or_404(config_id)
    if doc.get("is_active"):
        raise HTTPException(status_code=400, detail="Can't delete the active LLM config -- activate another one first")
    secret_manager.delete_secret(doc["secret_name"])
    firestore_db.delete_doc(_COLLECTION, config_id)


@router.post("/{config_id}/activate", response_model=LlmConfigOut)
def activate_llm_config(config_id: str):
    _get_or_404(config_id)
    for doc in firestore_db.list_docs(_COLLECTION):
        if doc.get("is_active") and doc["id"] != config_id:
            firestore_db.update_doc(_COLLECTION, doc["id"], {"is_active": False})
    firestore_db.update_doc(_COLLECTION, config_id, {"is_active": True})
    return _to_out(firestore_db.get_doc(_COLLECTION, config_id))

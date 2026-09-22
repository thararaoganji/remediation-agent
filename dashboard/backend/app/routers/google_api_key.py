"""Single global secret for Gemini access -- not a named list like Sonar
servers/GitHub credentials, since there's no "which one" choice for the
LLM call itself (see the dashboard plan's Architecture section)."""

from fastapi import APIRouter
from pydantic import BaseModel

from .. import secret_manager

router = APIRouter(prefix="/api/google-api-key", tags=["google-api-key"])

_SECRET_ID = "google-api-key"


class GoogleApiKeySet(BaseModel):
    value: str


class GoogleApiKeyStatus(BaseModel):
    configured: bool


@router.get("", response_model=GoogleApiKeyStatus)
def get_google_api_key_status():
    return GoogleApiKeyStatus(configured=secret_manager.secret_exists(_SECRET_ID))


@router.post("", status_code=204)
def set_google_api_key(body: GoogleApiKeySet):
    if secret_manager.secret_exists(_SECRET_ID):
        secret_manager.add_secret_version(_SECRET_ID, body.value)
    else:
        secret_manager.create_secret_with_value(_SECRET_ID, body.value)

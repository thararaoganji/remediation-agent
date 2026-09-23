"""Login/logout/current-user endpoints. Unauthenticated by design (login
obviously can't require being logged in already); everything else in the
app requires the session cookie these issue."""

import os

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import auth, firestore_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class CurrentUserOut(BaseModel):
    email: str
    role: str


def _cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "false").lower() == "true"


@router.post("/login", response_model=CurrentUserOut)
def login(body: LoginRequest, response: Response):
    doc = firestore_db.get_doc("users", body.email)
    if doc is None or not auth.verify_password(body.password, doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_cookie_value(body.email),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )
    return CurrentUserOut(email=body.email, role=doc["role"])


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)


@router.get("/me", response_model=CurrentUserOut)
def me(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return CurrentUserOut(email=user.email, role=user.role)

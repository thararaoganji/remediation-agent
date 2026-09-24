"""Login/logout/current-user/change-password endpoints. login, logout, and
change-password are unauthenticated-login aside -- change-password still
requires a valid session, it just doesn't require the caller to NOT be
mid-forced-reset (that's a frontend routing concern, not an API one)."""

import os

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr

from .. import auth, firestore_db
from ..validation import PasswordStr

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    # Deliberately plain str, not NonEmptyStr -- this gets compared
    # byte-for-byte against a stored hash, so it must never be silently
    # whitespace-stripped before that comparison. An empty password just
    # fails to match, which is already the correct outcome.
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str  # same reasoning as LoginRequest.password
    new_password: PasswordStr


class CurrentUserOut(BaseModel):
    email: str
    role: str
    must_reset_password: bool = False


def _cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "false").lower() == "true"


@router.post("/login", response_model=CurrentUserOut)
def login(body: LoginRequest, response: Response):
    email = body.email.lower()  # matches create_user's normalization -- see its comment
    doc = firestore_db.get_doc("users", email)
    if doc is None or not auth.verify_password(body.password, doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.create_session_cookie_value(email),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(),
    )
    return CurrentUserOut(email=email, role=doc["role"], must_reset_password=doc.get("must_reset_password", False))


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)


@router.get("/me", response_model=CurrentUserOut)
def me(user: auth.CurrentUser = Depends(auth.get_current_user)):
    return CurrentUserOut(email=user.email, role=user.role, must_reset_password=user.must_reset_password)


@router.post("/change-password", response_model=CurrentUserOut)
def change_password(body: ChangePasswordRequest, user: auth.CurrentUser = Depends(auth.get_current_user)):
    doc = firestore_db.get_doc("users", user.email)
    if doc is None or not auth.verify_password(body.current_password, doc["password_hash"]):
        # 400, not 401 -- the caller IS authenticated (a valid session got
        # them this far); this is a bad-input rejection, not an auth
        # failure, and the frontend's global 401 handler would otherwise
        # misread this as "your session expired" and bounce to /login.
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    firestore_db.update_doc("users", user.email, {
        "password_hash": auth.hash_password(body.new_password),
        "must_reset_password": False,
    })
    return CurrentUserOut(email=user.email, role=user.role, must_reset_password=False)

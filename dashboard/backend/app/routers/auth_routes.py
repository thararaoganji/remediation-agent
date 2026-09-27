"""Login/logout/current-user/change-password endpoints. login, logout, and
change-password are unauthenticated-login aside -- change-password still
requires a valid session, it just doesn't require the caller to NOT be
mid-forced-reset (that's a frontend routing concern, not an API one)."""

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

from .. import audit_log, auth, rate_limit, storage
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


def _client_ip(request: Request) -> str:
    # Both Cloud Run and Container Apps terminate TLS at their own edge
    # proxy, so request.client.host is that proxy's internal address (a
    # 169.254.x.x metadata-range IP on Cloud Run -- confirmed against real
    # production logs), never the real caller. X-Forwarded-For's first hop
    # is the actual client, same as any reverse-proxied deployment.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=CurrentUserOut)
def login(body: LoginRequest, response: Response, request: Request):
    email = body.email.lower()  # matches create_user's normalization -- see its comment
    client_ip = _client_ip(request)

    # Locked out by either key -- email (a distributed attack trying many
    # passwords against one account) or IP (one source trying many emails)
    # -- since either pattern alone is a real brute-force signal.
    if rate_limit.is_locked_out(email) or rate_limit.is_locked_out(f"ip:{client_ip}"):
        audit_log.login_locked_out(email, client_ip)
        raise HTTPException(status_code=429, detail="Too many failed login attempts -- try again in 15 minutes")

    doc = storage.get_doc("users", email)
    if doc is None or not auth.verify_password(body.password, doc["password_hash"]):
        rate_limit.record_failure(email)
        rate_limit.record_failure(f"ip:{client_ip}")
        audit_log.login_failed(email, client_ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    rate_limit.record_success(email)
    rate_limit.record_success(f"ip:{client_ip}")
    audit_log.login_succeeded(email, client_ip)
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
    doc = storage.get_doc("users", user.email)
    if doc is None or not auth.verify_password(body.current_password, doc["password_hash"]):
        # 400, not 401 -- the caller IS authenticated (a valid session got
        # them this far); this is a bad-input rejection, not an auth
        # failure, and the frontend's global 401 handler would otherwise
        # misread this as "your session expired" and bounce to /login.
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    storage.update_doc("users", user.email, {
        "password_hash": auth.hash_password(body.new_password),
        "must_reset_password": False,
    })
    audit_log.password_changed(user.email)
    return CurrentUserOut(email=user.email, role=user.role, must_reset_password=False)

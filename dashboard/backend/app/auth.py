"""Session-cookie auth for the dashboard backend -- a `users` Firestore
collection (doc id = email), bcrypt password hashes, and a signed session
cookie (itsdangerous, not a JWT library -- there's no cross-service token
exchange here, just "did our own backend issue this"). Cookies, not a
bearer token in a header, because EventTranscript.jsx's live transcript
uses a native EventSource (no custom headers possible) -- EventSource does
send cookies automatically on same-origin requests, so this works there
with zero frontend changes.

No account self-registration: SESSION_SECRET_KEY-signed cookies plus the
`users` collection are the only pieces here; account creation is either the
one-time create_admin.py bootstrap script or the admin-only /api/users
routes."""

import os

import bcrypt
from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from . import firestore_db

_COLLECTION = "users"
COOKIE_NAME = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(os.environ["SESSION_SECRET_KEY"], salt="dashboard-session")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_session_cookie_value(email: str) -> str:
    return _serializer().dumps({"email": email})


def _email_from_cookie(token: str) -> str | None:
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("email")


class CurrentUser(BaseModel):
    email: str
    role: str
    must_reset_password: bool = False


def get_current_user(session: str | None = Cookie(default=None)) -> CurrentUser:
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    email = _email_from_cookie(session)
    if email is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    doc = firestore_db.get_doc(_COLLECTION, email)
    if doc is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return CurrentUser(email=email, role=doc["role"], must_reset_password=doc.get("must_reset_password", False))


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

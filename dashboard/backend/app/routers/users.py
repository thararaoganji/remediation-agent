"""Admin-only account management -- there's no self-registration, so this
(plus the one-time create_admin.py bootstrap script) is the only way a
user account ever gets created."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from .. import auth, firestore_db
from ..validation import PasswordStr

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(auth.require_admin)])

_COLLECTION = "users"


class UserCreate(BaseModel):
    email: EmailStr
    password: PasswordStr
    role: Literal["admin", "user"] = "user"


class UserOut(BaseModel):
    email: str
    role: str
    must_reset_password: bool = False


def _to_out(doc: dict) -> UserOut:
    return UserOut(email=doc["email"], role=doc["role"], must_reset_password=doc.get("must_reset_password", False))


@router.get("", response_model=list[UserOut])
def list_users():
    return [_to_out(d) for d in firestore_db.list_docs(_COLLECTION, order_by="email")]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate):
    # Fully lowercased, not just EmailStr's domain-only normalization --
    # this is the Firestore doc id, and login looks up by the same
    # lowercased value, so a mismatch here would silently lock the new
    # user out if the admin typed any capital letters.
    email = body.email.lower()
    if firestore_db.get_doc(_COLLECTION, email) is not None:
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    # The admin is choosing this password on the new user's behalf, so it's
    # a temporary one by construction -- force it to be changed on first
    # login rather than assuming the admin communicated it securely enough
    # to just keep using.
    firestore_db.create_doc(_COLLECTION, {
        "email": email,
        "password_hash": auth.hash_password(body.password),
        "role": body.role,
        "must_reset_password": True,
    }, doc_id=email)
    return _to_out(firestore_db.get_doc(_COLLECTION, email))


@router.delete("/{email}", status_code=204)
def delete_user(email: str, current: auth.CurrentUser = Depends(auth.get_current_user)):
    if email == current.email:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    firestore_db.delete_doc(_COLLECTION, email)

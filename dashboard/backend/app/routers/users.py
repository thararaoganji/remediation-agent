"""Admin-only account management -- there's no self-registration, so this
(plus the one-time create_admin.py bootstrap script) is the only way a
user account ever gets created."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, firestore_db

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(auth.require_admin)])

_COLLECTION = "users"


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "user"  # "admin" | "user"


class UserOut(BaseModel):
    email: str
    role: str


@router.get("", response_model=list[UserOut])
def list_users():
    return [UserOut(email=d["email"], role=d["role"]) for d in firestore_db.list_docs(_COLLECTION, order_by="email")]


@router.post("", response_model=UserOut, status_code=201)
def create_user(body: UserCreate):
    if firestore_db.get_doc(_COLLECTION, body.email) is not None:
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    firestore_db.create_doc(_COLLECTION, {
        "email": body.email,
        "password_hash": auth.hash_password(body.password),
        "role": body.role,
    }, doc_id=body.email)
    return UserOut(email=body.email, role=body.role)


@router.delete("/{email}", status_code=204)
def delete_user(email: str, current: auth.CurrentUser = Depends(auth.get_current_user)):
    if email == current.email:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    firestore_db.delete_doc(_COLLECTION, email)

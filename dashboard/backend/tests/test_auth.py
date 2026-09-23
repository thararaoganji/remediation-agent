from conftest import login_as

from app import auth


def test_login_success_sets_cookie_and_returns_user(client, fake_firestore):
    fake_firestore.collection("users").document("alice@example.com").set({
        "email": "alice@example.com", "password_hash": auth.hash_password("secret123"), "role": "user",
    })
    resp = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "secret123"})
    assert resp.status_code == 200
    assert resp.json() == {"email": "alice@example.com", "role": "user"}
    assert auth.COOKIE_NAME in resp.cookies


def test_login_wrong_password_401s(client, fake_firestore):
    fake_firestore.collection("users").document("alice@example.com").set({
        "email": "alice@example.com", "password_hash": auth.hash_password("secret123"), "role": "user",
    })
    resp = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_email_401s(client, fake_firestore):
    resp = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401


def test_me_unauthenticated_401s(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_after_login(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com", role="admin")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"email": "alice@example.com", "role": "admin"}


def test_logout_clears_session(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.get("/api/auth/me").status_code == 200

    logout_resp = client.post("/api/auth/logout")
    assert logout_resp.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_deleted_user_session_401s(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    fake_firestore.collection("users").document("alice@example.com").delete()
    assert client.get("/api/auth/me").status_code == 401

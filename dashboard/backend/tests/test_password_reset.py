from conftest import _TEST_PASSWORD, login_as


def test_login_reflects_forced_reset_flag(client, fake_firestore):
    login_as(client, fake_firestore, "bob@example.com", must_reset_password=True)
    assert client.get("/api/auth/me").json()["must_reset_password"] is True


def test_login_reflects_no_reset_needed_by_default(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.get("/api/auth/me").json()["must_reset_password"] is False


def test_new_user_created_via_users_page_must_reset(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    created = client.post("/api/users", json={"email": "bob@example.com", "password": "temp-pass"}).json()
    assert created["must_reset_password"] is True


def test_change_password_requires_auth(client):
    resp = client.post("/api/auth/change-password", json={"current_password": "x", "new_password": "y"})
    assert resp.status_code == 401


def test_change_password_wrong_current_password_400s(client, fake_firestore):
    login_as(client, fake_firestore, "bob@example.com", must_reset_password=True)
    resp = client.post("/api/auth/change-password", json={"current_password": "wrong", "new_password": "new-pass-123"})
    assert resp.status_code == 400
    assert client.get("/api/auth/me").json()["must_reset_password"] is True


def test_change_password_success_clears_flag_and_updates_credentials(client, fake_firestore):
    login_as(client, fake_firestore, "bob@example.com", must_reset_password=True)
    resp = client.post("/api/auth/change-password", json={
        "current_password": _TEST_PASSWORD, "new_password": "new-pass-123",
    })
    assert resp.status_code == 200
    assert resp.json()["must_reset_password"] is False
    assert client.get("/api/auth/me").json()["must_reset_password"] is False

    # Logging in again requires the NEW password now.
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "bob@example.com", "password": _TEST_PASSWORD,
    }).status_code == 401
    ok = client.post("/api/auth/login", json={"email": "bob@example.com", "password": "new-pass-123"})
    assert ok.status_code == 200
    assert ok.json()["must_reset_password"] is False

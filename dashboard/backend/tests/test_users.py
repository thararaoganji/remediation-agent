from conftest import login_as


def test_requires_auth(client):
    assert client.get("/api/users").status_code == 401
    assert client.post("/api/users", json={"email": "a@x.com", "password": "p"}).status_code == 401


def test_non_admin_forbidden(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={"email": "a@x.com", "password": "p"}).status_code == 403
    assert client.delete("/api/users/alice@example.com").status_code == 403


def test_admin_can_list_create_and_delete_users(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")

    create_resp = client.post("/api/users", json={"email": "bob@example.com", "password": "password1", "role": "user"})
    assert create_resp.status_code == 201
    assert create_resp.json() == {"email": "bob@example.com", "role": "user", "must_reset_password": True}

    listed = {u["email"]: u["role"] for u in client.get("/api/users").json()}
    assert listed == {"admin@example.com": "admin", "bob@example.com": "user"}

    delete_resp = client.delete("/api/users/bob@example.com")
    assert delete_resp.status_code == 204
    assert [u["email"] for u in client.get("/api/users").json()] == ["admin@example.com"]


def test_creating_duplicate_email_409s(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    client.post("/api/users", json={"email": "bob@example.com", "password": "password1"})
    resp = client.post("/api/users", json={"email": "bob@example.com", "password": "password2"})
    assert resp.status_code == 409


def test_admin_cannot_delete_own_account(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.delete("/api/users/admin@example.com")
    assert resp.status_code == 400


def test_bootstrap_admin_via_login_as_is_not_forced_to_reset(client, fake_firestore):
    # login_as's default (must_reset_password=False) mirrors create_admin.py's
    # bootstrap script -- only users.py's create_user forces a reset.
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    listed = {u["email"]: u["must_reset_password"] for u in client.get("/api/users").json()}
    assert listed["admin@example.com"] is False

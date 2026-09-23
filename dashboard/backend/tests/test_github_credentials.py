from conftest import login_as


def test_create_uses_default_api_base_url(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/github-credentials", json={"name": "Acme Org", "token": "gh-tok"})
    assert resp.status_code == 201
    cred = resp.json()
    assert cred["api_base_url"] == "https://api.github.com"
    assert cred["owner_email"] == "alice@example.com"
    assert "token" not in cred


def test_create_with_enterprise_server_url(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/github-credentials", json={
        "name": "Internal GHES", "api_base_url": "https://ghe.internal.example.com/api/v3", "token": "ghes-tok",
    })
    cred = resp.json()
    assert cred["api_base_url"] == "https://ghe.internal.example.com/api/v3"
    secret_id = f"github-token-{cred['id']}"
    assert fake_secrets.secrets[secret_id][-1] == b"ghes-tok"


def test_rotate_token_and_delete(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "alice@example.com")
    cred = client.post("/api/github-credentials", json={"name": "Acme Org", "token": "gh-tok-1"}).json()
    secret_id = f"github-token-{cred['id']}"

    client.put(f"/api/github-credentials/{cred['id']}", json={"token": "gh-tok-2"})
    assert fake_secrets.secrets[secret_id][-1] == b"gh-tok-2"

    resp = client.delete(f"/api/github-credentials/{cred['id']}")
    assert resp.status_code == 204
    assert secret_id not in fake_secrets.secrets
    assert client.get("/api/github-credentials").json() == []


def test_unknown_credential_404s_on_update_and_delete(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.put("/api/github-credentials/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/github-credentials/nope").status_code == 404


def test_github_credentials_require_auth(client):
    assert client.get("/api/github-credentials").status_code == 401
    assert client.post("/api/github-credentials", json={"name": "A", "token": "t"}).status_code == 401


def test_user_cannot_see_or_manage_another_users_credential(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    cred_id = client.post("/api/github-credentials", json={"name": "Alice's", "token": "ta"}).json()["id"]

    login_as(client, fake_firestore, "bob@example.com")
    assert client.get("/api/github-credentials").json() == []
    assert client.put(f"/api/github-credentials/{cred_id}", json={"name": "hijacked"}).status_code == 404
    assert client.delete(f"/api/github-credentials/{cred_id}").status_code == 404


def test_admin_sees_and_manages_every_users_credential(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    cred_id = client.post("/api/github-credentials", json={"name": "Alice's", "token": "ta"}).json()["id"]

    login_as(client, fake_firestore, "admin@example.com", role="admin")
    listed = client.get("/api/github-credentials").json()
    assert [c["id"] for c in listed] == [cred_id]
    assert client.put(f"/api/github-credentials/{cred_id}", json={"name": "renamed by admin"}).status_code == 200
    assert client.delete(f"/api/github-credentials/{cred_id}").status_code == 204

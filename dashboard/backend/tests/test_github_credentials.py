def test_create_uses_default_api_base_url(client):
    resp = client.post("/api/github-credentials", json={"name": "Acme Org", "token": "gh-tok"})
    assert resp.status_code == 201
    cred = resp.json()
    assert cred["api_base_url"] == "https://api.github.com"
    assert "token" not in cred


def test_create_with_enterprise_server_url(client, fake_secrets):
    resp = client.post("/api/github-credentials", json={
        "name": "Internal GHES", "api_base_url": "https://ghe.internal.example.com/api/v3", "token": "ghes-tok",
    })
    cred = resp.json()
    assert cred["api_base_url"] == "https://ghe.internal.example.com/api/v3"
    secret_id = f"github-token-{cred['id']}"
    assert fake_secrets.secrets[secret_id][-1] == b"ghes-tok"


def test_rotate_token_and_delete(client, fake_secrets):
    cred = client.post("/api/github-credentials", json={"name": "Acme Org", "token": "gh-tok-1"}).json()
    secret_id = f"github-token-{cred['id']}"

    client.put(f"/api/github-credentials/{cred['id']}", json={"token": "gh-tok-2"})
    assert fake_secrets.secrets[secret_id][-1] == b"gh-tok-2"

    resp = client.delete(f"/api/github-credentials/{cred['id']}")
    assert resp.status_code == 204
    assert secret_id not in fake_secrets.secrets
    assert client.get("/api/github-credentials").json() == []


def test_unknown_credential_404s_on_update_and_delete(client):
    assert client.put("/api/github-credentials/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/github-credentials/nope").status_code == 404

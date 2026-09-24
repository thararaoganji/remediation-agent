from conftest import login_as


def test_requires_auth(client):
    assert client.get("/api/llm-configs").status_code == 401


def test_non_admin_forbidden(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.get("/api/llm-configs").status_code == 403
    assert client.post("/api/llm-configs", json={"vendor": "google", "model": "x", "api_key": "k"}).status_code == 403


def test_first_config_created_is_auto_activated(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"})
    assert resp.status_code == 201
    config = resp.json()
    assert config["is_active"] is True
    assert fake_secrets.secrets[f"llm-api-key-{config['id']}"][-1] == b"k1"


def test_second_config_created_is_not_active(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    first = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"}).json()
    second = client.post("/api/llm-configs", json={"vendor": "openai", "model": "gpt-4o", "api_key": "k2"}).json()
    assert first["is_active"] is True
    assert second["is_active"] is False


def test_activate_deactivates_the_previous_one(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    first = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"}).json()
    second = client.post("/api/llm-configs", json={"vendor": "openai", "model": "gpt-4o", "api_key": "k2"}).json()

    resp = client.post(f"/api/llm-configs/{second['id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    listed = {c["id"]: c["is_active"] for c in client.get("/api/llm-configs").json()}
    assert listed[first["id"]] is False
    assert listed[second["id"]] is True


def test_cannot_delete_the_active_config(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    config = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"}).json()
    resp = client.delete(f"/api/llm-configs/{config['id']}")
    assert resp.status_code == 400


def test_can_delete_an_inactive_config(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"})
    second = client.post("/api/llm-configs", json={"vendor": "openai", "model": "gpt-4o", "api_key": "k2"}).json()

    resp = client.delete(f"/api/llm-configs/{second['id']}")
    assert resp.status_code == 204
    assert f"llm-api-key-{second['id']}" not in fake_secrets.secrets


def test_update_rotates_token_and_edits_fields(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    config = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini-3.7-flash", "api_key": "k1"}).json()

    resp = client.put(f"/api/llm-configs/{config['id']}", json={"model": "gemini-2.5-pro", "api_key": "k2"})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gemini-2.5-pro"
    assert fake_secrets.secrets[f"llm-api-key-{config['id']}"] == [b"k1", b"k2"]


def test_unknown_vendor_rejected(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/llm-configs", json={"vendor": "carrier-pigeon", "model": "x", "api_key": "k"})
    assert resp.status_code == 422  # rejected by the Vendor Literal type before the handler runs

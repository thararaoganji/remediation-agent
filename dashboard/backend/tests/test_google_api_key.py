from conftest import login_as


def test_initially_not_configured(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    assert client.get("/api/google-api-key").json() == {"configured": False}


def test_set_then_configured(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/google-api-key", json={"value": "gemini-key-1"})
    assert resp.status_code == 204
    assert client.get("/api/google-api-key").json() == {"configured": True}
    assert fake_secrets.secrets["google-api-key"][-1] == b"gemini-key-1"


def test_setting_again_rotates_not_duplicates(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    client.post("/api/google-api-key", json={"value": "key-1"})
    client.post("/api/google-api-key", json={"value": "key-2"})
    assert fake_secrets.secrets["google-api-key"] == [b"key-1", b"key-2"]


def test_requires_auth(client):
    assert client.get("/api/google-api-key").status_code == 401
    assert client.post("/api/google-api-key", json={"value": "x"}).status_code == 401


def test_non_admin_forbidden_on_both_endpoints(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    assert client.get("/api/google-api-key").status_code == 403
    assert client.post("/api/google-api-key", json={"value": "x"}).status_code == 403

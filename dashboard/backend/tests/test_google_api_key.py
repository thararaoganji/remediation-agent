def test_initially_not_configured(client):
    assert client.get("/api/google-api-key").json() == {"configured": False}


def test_set_then_configured(client, fake_secrets):
    resp = client.post("/api/google-api-key", json={"value": "gemini-key-1"})
    assert resp.status_code == 204
    assert client.get("/api/google-api-key").json() == {"configured": True}
    assert fake_secrets.secrets["google-api-key"][-1] == b"gemini-key-1"


def test_setting_again_rotates_not_duplicates(client, fake_secrets):
    client.post("/api/google-api-key", json={"value": "key-1"})
    client.post("/api/google-api-key", json={"value": "key-2"})
    assert fake_secrets.secrets["google-api-key"] == [b"key-1", b"key-2"]

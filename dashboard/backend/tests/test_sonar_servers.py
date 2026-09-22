def test_create_list_update_delete_sonar_server(client, fake_secrets):
    resp = client.post("/api/sonar-servers", json={
        "name": "Prod Sonar", "base_url": "http://sonar.prod:9000", "ce_edition": True, "token": "tok-1",
    })
    assert resp.status_code == 201
    server = resp.json()
    assert server["name"] == "Prod Sonar"
    assert server["base_url"] == "http://sonar.prod:9000"
    assert server["ce_edition"] is True
    assert "token" not in server  # token value never comes back out

    server_id = server["id"]
    secret_id = f"sonar-token-{server_id}"
    assert fake_secrets.secrets[secret_id][-1] == b"tok-1"

    listed = client.get("/api/sonar-servers").json()
    assert [s["id"] for s in listed] == [server_id]

    update_resp = client.put(f"/api/sonar-servers/{server_id}", json={"name": "Prod Sonar (renamed)", "token": "tok-2"})
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Prod Sonar (renamed)"
    assert fake_secrets.secrets[secret_id][-1] == b"tok-2"
    assert len(fake_secrets.secrets[secret_id]) == 2  # rotation adds a version, doesn't replace

    delete_resp = client.delete(f"/api/sonar-servers/{server_id}")
    assert delete_resp.status_code == 204
    assert client.get("/api/sonar-servers").json() == []
    assert secret_id not in fake_secrets.secrets


def test_update_unknown_sonar_server_404s(client):
    resp = client.put("/api/sonar-servers/does-not-exist", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_unknown_sonar_server_404s(client):
    resp = client.delete("/api/sonar-servers/does-not-exist")
    assert resp.status_code == 404


def test_two_sonar_servers_are_independent(client):
    a = client.post("/api/sonar-servers", json={"name": "A", "base_url": "http://a", "token": "ta"}).json()
    b = client.post("/api/sonar-servers", json={"name": "B", "base_url": "http://b", "token": "tb"}).json()
    assert a["id"] != b["id"]
    names = {s["name"] for s in client.get("/api/sonar-servers").json()}
    assert names == {"A", "B"}

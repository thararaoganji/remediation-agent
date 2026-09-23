from conftest import login_as


def test_create_list_update_delete_sonar_server(client, fake_firestore, fake_secrets):
    login_as(client, fake_firestore, "alice@example.com")

    resp = client.post("/api/sonar-servers", json={
        "name": "Prod Sonar", "base_url": "http://sonar.prod:9000", "ce_edition": True, "token": "tok-1",
    })
    assert resp.status_code == 201
    server = resp.json()
    assert server["name"] == "Prod Sonar"
    assert server["base_url"] == "http://sonar.prod:9000"
    assert server["ce_edition"] is True
    assert server["owner_email"] == "alice@example.com"
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


def test_update_unknown_sonar_server_404s(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.put("/api/sonar-servers/does-not-exist", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_unknown_sonar_server_404s(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.delete("/api/sonar-servers/does-not-exist")
    assert resp.status_code == 404


def test_two_sonar_servers_are_independent(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    a = client.post("/api/sonar-servers", json={"name": "A", "base_url": "http://a", "token": "ta"}).json()
    b = client.post("/api/sonar-servers", json={"name": "B", "base_url": "http://b", "token": "tb"}).json()
    assert a["id"] != b["id"]
    names = {s["name"] for s in client.get("/api/sonar-servers").json()}
    assert names == {"A", "B"}


def test_sonar_servers_require_auth(client):
    assert client.get("/api/sonar-servers").status_code == 401
    assert client.post("/api/sonar-servers", json={"name": "A", "base_url": "http://a", "token": "t"}).status_code == 401


def test_user_cannot_see_or_manage_another_users_sonar_server(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    server_id = client.post(
        "/api/sonar-servers", json={"name": "Alice's", "base_url": "http://a", "token": "ta"}
    ).json()["id"]

    login_as(client, fake_firestore, "bob@example.com")
    assert client.get("/api/sonar-servers").json() == []
    assert client.put(f"/api/sonar-servers/{server_id}", json={"name": "hijacked"}).status_code == 404
    assert client.delete(f"/api/sonar-servers/{server_id}").status_code == 404


def test_admin_sees_and_manages_every_users_sonar_server(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    server_id = client.post(
        "/api/sonar-servers", json={"name": "Alice's", "base_url": "http://a", "token": "ta"}
    ).json()["id"]

    login_as(client, fake_firestore, "admin@example.com", role="admin")
    listed = client.get("/api/sonar-servers").json()
    assert [s["id"] for s in listed] == [server_id]
    assert client.put(f"/api/sonar-servers/{server_id}", json={"name": "renamed by admin"}).status_code == 200
    assert client.delete(f"/api/sonar-servers/{server_id}").status_code == 204

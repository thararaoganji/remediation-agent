"""Request-validation behavior added across every router -- empty/whitespace
strings, malformed URLs/emails, too-short passwords, and unknown
enum-like values (role/vendor/agent_type/source_type) should all 422
before any handler code runs, not fail later or silently store garbage."""

from conftest import login_as


# --- Sonar servers ----------------------------------------------------

def test_sonar_server_rejects_empty_name(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/sonar-servers", json={"name": "", "base_url": "http://sonar.example.com", "token": "t"})
    assert resp.status_code == 422


def test_sonar_server_rejects_whitespace_only_name(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/sonar-servers", json={"name": "   ", "base_url": "http://sonar.example.com", "token": "t"})
    assert resp.status_code == 422


def test_sonar_server_rejects_non_url_base_url(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/sonar-servers", json={"name": "Prod", "base_url": "not-a-url", "token": "t"})
    assert resp.status_code == 422


def test_sonar_server_rejects_empty_token(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/sonar-servers", json={"name": "Prod", "base_url": "http://sonar.example.com", "token": ""})
    assert resp.status_code == 422


def test_sonar_server_update_rejects_bad_url(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    server = client.post(
        "/api/sonar-servers", json={"name": "Prod", "base_url": "http://sonar.example.com", "token": "t"},
    ).json()
    resp = client.put(f"/api/sonar-servers/{server['id']}", json={"base_url": "ftp://not-http"})
    assert resp.status_code == 422


# --- GitHub credentials -------------------------------------------------

def test_github_credential_rejects_empty_name(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/github-credentials", json={"name": "  ", "token": "t"})
    assert resp.status_code == 422


def test_github_credential_rejects_non_url_api_base_url(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/github-credentials", json={"name": "Acme", "api_base_url": "example.com", "token": "t"})
    assert resp.status_code == 422


# --- LLM configs ----------------------------------------------------------

def test_llm_config_rejects_empty_model(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/llm-configs", json={"vendor": "google", "model": "", "api_key": "k"})
    assert resp.status_code == 422


def test_llm_config_rejects_empty_api_key(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/llm-configs", json={"vendor": "google", "model": "gemini", "api_key": ""})
    assert resp.status_code == 422


# --- Users ------------------------------------------------------------

def test_user_create_rejects_malformed_email(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/users", json={"email": "not-an-email", "password": "password1"})
    assert resp.status_code == 422


def test_user_create_rejects_short_password(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/users", json={"email": "bob@example.com", "password": "short"})
    assert resp.status_code == 422


def test_user_create_rejects_unknown_role(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    resp = client.post("/api/users", json={"email": "bob@example.com", "password": "password1", "role": "superuser"})
    assert resp.status_code == 422


def test_user_create_normalizes_email_case(client, fake_firestore):
    login_as(client, fake_firestore, "admin@example.com", role="admin")
    created = client.post("/api/users", json={"email": "Bob@Example.com", "password": "password1"}).json()
    assert created["email"] == "bob@example.com"

    # And can log in with any casing of that same address.
    login_resp = client.post("/api/auth/login", json={"email": "BOB@EXAMPLE.COM", "password": "password1"})
    assert login_resp.status_code == 200
    assert login_resp.json()["email"] == "bob@example.com"


# --- Auth ---------------------------------------------------------------

def test_login_rejects_malformed_email(client):
    resp = client.post("/api/auth/login", json={"email": "not-an-email", "password": "x"})
    assert resp.status_code == 422


def test_change_password_rejects_short_new_password(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/auth/change-password", json={
        "current_password": "test-password-123", "new_password": "short",
    })
    assert resp.status_code == 422


# --- Runs -----------------------------------------------------------------

def test_create_run_rejects_unknown_agent_type(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/runs", json={
        "agent_type": "not-a-real-agent", "source_type": "local", "source": "/x", "sonar_server_id": "x",
    })
    assert resp.status_code == 422


def test_create_run_rejects_unknown_source_type(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "ftp", "source": "/x", "sonar_server_id": "x",
    })
    assert resp.status_code == 422


def test_create_run_rejects_empty_source(client, fake_firestore):
    login_as(client, fake_firestore, "alice@example.com")
    resp = client.post("/api/runs", json={
        "agent_type": "techdebt", "source_type": "local", "source": "  ", "sonar_server_id": "x",
    })
    assert resp.status_code == 422

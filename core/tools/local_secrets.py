"""Resolves run_local.py's secret-bearing env vars (SONAR_TOKEN,
GITHUB_TOKEN, the active LLM vendor's key) directly from the dashboard's
own MongoDB -- the same sonar_servers/github_credentials/llm_configs/
_secrets collections dashboard/backend/app/routers/runs.py's create_run()
resolves them from when the dashboard's "New Run" button starts a job,
just read straight out of Mongo here instead of through the FastAPI app.

Lets `docker compose run agent` use a Sonar server / GitHub credential /
LLM key you already saved on the Connections page, by NAME, instead of
either going through the whole dashboard (backend + Docker-in-Docker) or
hand-copying a raw secret value out of mongosh for every run.

Opt-in only: apply_to_environ() is a no-op unless CLOUD_PROVIDER=local AND
SONAR_SERVER_NAME is set, so every other way of running this agent (GCP,
Azure, or a local run with secrets already supplied as plain env vars) is
completely unaffected -- same "unaffected unless explicitly opted in"
contract core/tools/run_status.py's providers already follow.

Deliberately duplicates the dashboard backend's own resolution logic
(routers/runs.py's create_run) rather than importing it: dashboard/
backend/app is a separate package with its own requirements (FastAPI,
etc.) not installed in this image. Looks connections up by their "name"
field, matching secrets_local.py's own "deliberately NOT a real secret
store" scope -- fine for a single local developer's own saved
connections, not a multi-tenant lookup (two different owners could in
principle reuse the same connection name; this takes whichever Mongo
returns first)."""

import os

from pymongo import MongoClient

from core import llm_config


class LocalSecretLookupError(Exception):
    """A SONAR_SERVER_NAME/GITHUB_CREDENTIAL_NAME that doesn't match
    anything saved, or no active LLM config at all -- always a config
    mistake (wrong name, nothing added yet via the Connections page),
    never something retrying would fix."""


def _db():
    client = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    return client[os.environ.get("MONGO_DB", "dashboard")]


def _secret_value(db, secret_name: str) -> str:
    doc = db["_secrets"].find_one({"_id": secret_name})
    if not doc or not doc.get("versions"):
        raise LocalSecretLookupError(f"No secret value stored for {secret_name!r}")
    return doc["versions"][-1]


def _by_name(db, collection: str, name: str) -> dict:
    doc = db[collection].find_one({"name": name})
    if doc is None:
        raise LocalSecretLookupError(
            f"No {collection} connection named {name!r} -- check the exact name on the dashboard's Connections page"
        )
    return doc


def resolve_env() -> dict[str, str]:
    """Reads SONAR_SERVER_NAME (required) and GITHUB_CREDENTIAL_NAME
    (optional) plus whichever llm_configs doc is currently marked active,
    and returns the env vars run_local.py needs for all three:
    SONAR_BASE_URL/SONAR_TOKEN/CE_EDITION, GITHUB_TOKEN (if a github
    credential name was given), and LLM_VENDOR/LLM_MODEL plus whichever
    env var that vendor's key belongs in (e.g. GOOGLE_API_KEY)."""
    db = _db()
    env: dict[str, str] = {}

    sonar = _by_name(db, "sonar_servers", os.environ["SONAR_SERVER_NAME"])
    env["SONAR_BASE_URL"] = sonar["base_url"]
    env["CE_EDITION"] = "true" if sonar["ce_edition"] else "false"
    env["SONAR_TOKEN"] = _secret_value(db, sonar["secret_name"])

    github_name = os.environ.get("GITHUB_CREDENTIAL_NAME")
    if github_name:
        github_cred = _by_name(db, "github_credentials", github_name)
        env["GITHUB_TOKEN"] = _secret_value(db, github_cred["secret_name"])

    llm = db["llm_configs"].find_one({"is_active": True})
    if llm is None:
        raise LocalSecretLookupError("No active LLM config in Mongo -- add one on the Connections page and activate it")
    env["LLM_VENDOR"] = llm["vendor"]
    env["LLM_MODEL"] = llm["model"]
    env[llm_config.env_var_for_vendor(llm["vendor"])] = _secret_value(db, llm["secret_name"])

    return env


def apply_to_environ(environ=os.environ) -> None:
    """Call once, right after load_dotenv() and before anything reads
    SONAR_TOKEN/LLM_VENDOR/etc. -- setdefault, not a plain assignment, so
    an env var already set explicitly (e.g. passed straight on a `docker
    compose run -e` line) always wins over whatever's stored in Mongo,
    same per-run-wins precedence job_runner_local.py already uses."""
    if environ.get("CLOUD_PROVIDER") != "local" or not environ.get("SONAR_SERVER_NAME"):
        return
    for key, value in resolve_env().items():
        environ.setdefault(key, value)

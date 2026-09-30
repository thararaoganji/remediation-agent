# Running the dashboard locally

Two ways to run this locally, depending on what you're testing:

- **Fully local, no cloud account at all** (`docker compose up`, below) --
  MongoDB stands in for Firestore/Cosmos DB, and "New Run" starts a real
  sibling Docker container for the agent instead of a Cloud Run Job/
  Container Apps Job. Good for dashboard/API development, or exercising a
  real end-to-end run against a real SonarQube server + GitHub repo + LLM
  key without touching GCP or Azure at all.
- **Against real GCP** (the "Backend"/"Frontend" sections further down) --
  the actual web app runs on your machine, but `POST /api/runs` calls the
  real Cloud Run Admin API, so the remediation work itself still runs on
  Cloud Run. Useful for testing against production-shaped infrastructure.

To run the agent itself directly with no dashboard involved at all, skip
this directory entirely and use `run_local.py` at the repo root instead --
see the main [README.md](../README.md)'s "Setup (macOS)" section.

## Fully local (Docker Compose, no cloud account needed)

```bash
docker compose build            # builds the agent image "New Run" starts sibling containers from
docker compose up               # starts mongo + backend + frontend
```

Then, once (per fresh `mongo` volume):

```bash
docker compose exec backend python create_admin.py --email you@example.com --password 'a-real-password'
```

Open http://localhost:5180, log in, and add a Sonar server + GitHub
credential + LLM API key from the Connections page like normal -- these are
real connections (a real SonarQube server, a real GitHub repo, a real LLM
key), just backed by a local MongoDB instead of Firestore/Cosmos DB. "New
Run" spawns a real sibling container from the agent image on the same
Docker network as `mongo`, exactly like Cloud Run Jobs/Container Apps Jobs
do in production, just on your own machine -- confirmed end to end
(login, connections, a real run reaching a real `git clone` attempt,
status/events landing back in Mongo, the SSE stream, the built frontend
proxying `/api/*` to the backend container) while building this.

Needs the host's Docker socket, so it only works where that's available
(a normal Docker Desktop/Docker Engine install; not inside another
sandboxed container with no socket access). `docker compose down -v` wipes
the Mongo volume for a clean slate; leave off `-v` to keep your data across
restarts.

## Prerequisites for the dashboard backend (real GCP)

The backend needs a real GCP project for Firestore, Secret Manager, and
Cloud Run Jobs if you're not using the fully-local Docker Compose setup
above.

```bash
gcloud auth application-default login   # so google.cloud.* clients pick up your credentials
gcloud services enable firestore.googleapis.com secretmanager.googleapis.com run.googleapis.com
gcloud firestore databases create --location=REGION   # once per project, if not already done
```

You'll also want the three agent Cloud Run Jobs already deployed (§2 of
`../docs/GCP_DEPLOYMENT.md`) if you actually want "Start run" to do
something real, and at least one Sonar server + one GitHub credential added
via the Connections page before "New Run" has anything to select.

## Backend

```bash
cd dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GCP_PROJECT_ID=your-project-id
export GCP_REGION=us-central1   # wherever you deployed the three Jobs
export SESSION_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")   # signs login session cookies -- keep this stable across restarts, or everyone gets logged out
# export COOKIE_SECURE=true   # only once served over https (e.g. Phase D's Cloud Run Service) -- leave unset for local http

uvicorn app.main:app --reload --port 8000
```

`--reload` picks up code changes without restarting. Confirm it's up:

```bash
curl http://localhost:8000/api/health   # {"status": "ok"}
```

### Authentication

There's no self-signup. The first time you set this up (per Firestore
project), bootstrap an admin account once:

```bash
python create_admin.py --email you@example.com --password 'a-real-password'
```

Log in at the frontend's `/login` page with that account, then create
further accounts (admin or user) from the "Users" nav link. Admins get the
Google API Key section on the Connections page and see every user's runs
and connections; users only see/manage their own.

Run its own test suite (no real GCP needed -- everything's mocked, see
`tests/conftest.py`):

```bash
pytest   # from dashboard/backend/
```

## Frontend

```bash
cd dashboard/frontend
npm install
npm run dev
```

Open the URL Vite prints (typically http://localhost:5173). `vite.config.js`
proxies `/api/*` to `http://localhost:8000` in dev mode, so the backend above
needs to already be running. In production (Phase D) the built frontend is
served BY the FastAPI backend itself, same origin -- this proxy is dev-only.

## Running the frontend with no backend at all

The pages still render without a live backend or real GCP credentials behind
it -- Connections/New Run show "Failed to load: ..." for anything
Firestore-backed, and the Google API key status shows "not configured"
(Secret Manager access failures and "secret doesn't exist" are
indistinguishable by design, see `secret_manager.secret_exists()`). Useful
for frontend-only UI work, not for testing an actual run.

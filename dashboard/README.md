# Running the dashboard locally

Two pieces, both able to run on your own machine: `backend/` (FastAPI) and
`frontend/` (React + Vite). Important nuance up front:

**Even running locally, the backend triggers real Cloud Run Job executions on
GCP** — `POST /api/runs` calls the real Cloud Run Admin API
(`app/cloud_run.py`), not a local subprocess. There is no "run the agent
locally through the dashboard" path. Running the dashboard locally only means
the *web app* (UI + API) runs on your machine; the actual remediation work it
kicks off still runs on Cloud Run, same as `gcloud run jobs execute` would.
See [../docs/GCP_DEPLOYMENT.md](../docs/GCP_DEPLOYMENT.md) for deploying
those three jobs first.

To run the agent itself fully locally, with no cloud dependency at all, skip
this directory entirely and use `run_local.py` at the repo root instead --
see the main [README.md](../README.md)'s "Setup (macOS)" section.

## Prerequisites for the dashboard backend

The backend needs a real GCP project for Firestore, Secret Manager, and
Cloud Run Jobs -- there's no local emulator wired up (the Phase B plan notes
a Firestore emulator as the intended *automated-test* setup; manual local
running against real GCP is simpler for now and mirrors production).

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

uvicorn app.main:app --reload --port 8000
```

`--reload` picks up code changes without restarting. Confirm it's up:

```bash
curl http://localhost:8000/api/health   # {"status": "ok"}
```

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

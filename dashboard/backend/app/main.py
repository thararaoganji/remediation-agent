"""FastAPI app for the Sonar remediation dashboard backend. Phase D adds
serving the built React frontend's static assets alongside these /api/*
routes from this same app (one Cloud Run Service, no CORS) -- API-only
for now so this phase is independently testable, per the dashboard plan."""

from fastapi import FastAPI

from .routers import github_credentials, google_api_key, runs, sonar_servers

app = FastAPI(title="Sonar Remediation Dashboard API")

app.include_router(sonar_servers.router)
app.include_router(github_credentials.router)
app.include_router(google_api_key.router)
app.include_router(runs.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

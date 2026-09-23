"""FastAPI app for the Sonar remediation dashboard backend. Phase D adds
serving the built React frontend's static assets alongside these /api/*
routes from this same app (one Cloud Run Service, no CORS) -- API-only
for now so this phase is independently testable, per the dashboard plan.

Every router below except auth_routes (can't require login to log in) and
/api/health requires a valid session; llm_configs and users additionally
require the admin role -- see app/auth.py."""

from fastapi import Depends, FastAPI

from . import auth
from .routers import auth_routes, github_credentials, llm_configs, runs, sonar_servers, users

app = FastAPI(title="Sonar Remediation Dashboard API")

app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(llm_configs.router)
app.include_router(sonar_servers.router, dependencies=[Depends(auth.get_current_user)])
app.include_router(github_credentials.router, dependencies=[Depends(auth.get_current_user)])
app.include_router(runs.router, dependencies=[Depends(auth.get_current_user)])


@app.get("/api/health")
def health():
    return {"status": "ok"}

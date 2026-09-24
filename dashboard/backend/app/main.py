"""FastAPI app for the Sonar remediation dashboard backend. Phase D serves
the built React frontend's static assets alongside these /api/* routes from
this same app (one Cloud Run Service, no CORS) -- see the _STATIC_DIR block
at the bottom of this file.

Every router below except auth_routes (can't require login to log in) and
/api/health requires a valid session; llm_configs and users additionally
require the admin role -- see app/auth.py."""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


# dashboard/Dockerfile copies the frontend's built dist/ here, so the
# deployed image serves the UI from the same origin as /api/* -- no CORS
# needed. Locally this directory doesn't exist (nobody builds the frontend
# into the backend for day-to-day dev, see dashboard/README.md), so both
# routes below are skipped and local dev stays API-only, same as before.
# Registered last so it never shadows an /api/* route above.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # Every client-side route (e.g. /runs/abc123) should load the same
        # index.html and let React Router take over -- otherwise a hard
        # refresh on anything but "/" 404s.
        return FileResponse(_STATIC_DIR / "index.html")

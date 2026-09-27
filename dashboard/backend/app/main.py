"""FastAPI app for the Sonar remediation dashboard backend. Phase D serves
the built React frontend's static assets alongside these /api/* routes from
this same app (one Cloud Run Service, no CORS) -- see the _STATIC_DIR block
at the bottom of this file.

Every router below except auth_routes (can't require login to log in) and
/api/health requires a valid session; llm_configs and users additionally
require the admin role -- see app/auth.py."""

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import auth
from .routers import auth_routes, github_credentials, llm_configs, runs, sonar_servers, users

app = FastAPI(title="Sonar Remediation Dashboard API")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """OWASP A05 (Security Misconfiguration): every response gets a
    baseline set of browser security headers. No 'unsafe-inline' anywhere
    in the CSP -- StatusBadge.jsx and ThemeContext.jsx were the only two
    places that used to need it (inline style= / element.style.* JS), both
    refactored to plain CSS classes / data-theme attributes instead."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        # Reads X-Forwarded-Proto directly rather than trusting
        # request.url.scheme -- both Cloud Run and Container Apps terminate
        # TLS at their own edge and proxy to this container over plain
        # HTTP, and uvicorn only rewrites the scope's scheme from that
        # header when the immediate TCP peer is in --forwarded-allow-ips
        # (default just "127.0.0.1"), which the platform's real internal
        # proxy IP never is. Confirmed empirically: request.url.scheme
        # stayed "http" against a simulated proxied request until this was
        # read directly -- relying on the uvicorn default here would have
        # meant HSTS silently never fired on any real deployment, on
        # either cloud, while looking correct in every local test.
        is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        if is_https:
            # Only when actually served over HTTPS -- asserting this over
            # plain local-dev HTTP would be a lie the browser then caches.
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

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

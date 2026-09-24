# Builds the dashboard as ONE Cloud Run Service: the frontend is built to
# static assets in stage 1, then copied into the FastAPI app's static/ dir
# in stage 2, which is what app/main.py's Phase D block serves alongside
# /api/* -- same origin, no CORS. This never runs the agents themselves
# (that's the separate root Dockerfile / Cloud Run Jobs), so it doesn't
# need Java/Maven/Gradle -- just Node to build, then Python to serve.

FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY dashboard/frontend/package.json dashboard/frontend/package-lock.json ./
RUN npm ci
COPY dashboard/frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app

COPY dashboard/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/backend/ .
COPY --from=frontend-build /frontend/dist/ ./app/static/

# Cloud Run injects $PORT (default 8080) and expects the container to listen
# on it -- it is NOT necessarily 8080 in every environment, so this must be
# read at start time rather than hardcoded.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

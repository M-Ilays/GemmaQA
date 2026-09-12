# GemmaQA production image for Google Cloud Run (one service: API + SPA + Playwright).
# Build from the repository root:
#   docker build -t gemmaqa:local .
# Do not copy secrets. Pass GEMINI_API_KEY at runtime (Cloud Run Secret Manager).

# ---------------------------------------------------------------------------
# Frontend (Vite production build — same-origin / relative API + WS URLs)
# ---------------------------------------------------------------------------
FROM node:20-bookworm-slim AS frontend

WORKDIR /src
COPY gemmaqa/frontend/package.json gemmaqa/frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY gemmaqa/frontend/ ./
# Empty VITE_API_BASE_URL / VITE_WS_BASE_URL → relative /api and wss://window.location.host
ENV VITE_API_BASE_URL=
ENV VITE_WS_BASE_URL=
ENV VITE_DEMO_MODE=false
RUN npm run build

# ---------------------------------------------------------------------------
# Backend + Chromium + baked SPA
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY gemmaqa/backend/requirements.txt /tmp/requirements.txt
COPY gemmaqa/backend/requirements-gemini.txt /tmp/requirements-gemini.txt
RUN pip install --no-cache-dir \
        -r /tmp/requirements.txt \
        -r /tmp/requirements-gemini.txt \
    && playwright install --with-deps chromium \
    && rm -rf /root/.cache/pip

# Layout matches local project_root (config.py parents[2]):
#   /app/backend/app/config.py  →  project_root = /app
COPY gemmaqa/backend /app/backend
COPY --from=frontend /src/dist /app/frontend/dist

# Writable runtime dirs (SQLite + evidence are ephemeral on Cloud Run)
RUN mkdir -p /app/evidence /app/backend \
    && chmod -R a+rwx /app/evidence

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV HOST=0.0.0.0
ENV PORT=8080
ENV GEMMA_PROVIDER=gemini
ENV GEMINI_MODEL_ID=gemini-3.5-flash
ENV GEMMA_MAX_OUTPUT_TOKENS=4096
ENV BROWSER_ADAPTER=direct_playwright
ENV PLAYWRIGHT_HEADLESS=true
ENV PLAYWRIGHT_DOCKER=1
ENV SERVE_FRONTEND=1
ENV DEBUG=false
ENV ALLOW_LOCAL_TARGETS=false

WORKDIR /app/backend
EXPOSE 8080

# Cloud Run sets PORT. Do not bake GEMINI_API_KEY.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

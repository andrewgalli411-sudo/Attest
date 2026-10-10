# Backend: API + the Playwright/Chromium the audits drive. Runs as a non-root user.
# The image tag MUST match the playwright version in uv.lock (1.63.0): the library drives that exact browser build.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv==0.5.* && \
    apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs && \
    npm install -g pnpm@10 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY vendor ./vendor
COPY alembic.ini ./
COPY alembic ./alembic
RUN uv sync --frozen --no-dev
RUN useradd --create-home --uid 10001 auditor && mkdir -p /data && chown -R auditor /data /app
USER auditor
ENV EVIDENCE_DIR=/data/evidence DATABASE_URL=sqlite:////data/auditor.db HOME=/home/auditor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
# Migrate, then serve. The schema is never auto-created in production.
CMD ["sh", "-c", "uv run alembic upgrade head && uv run python -m auditor.bootstrap && exec uv run uvicorn auditor.main:create_app --factory --host 0.0.0.0 --port 8000"]

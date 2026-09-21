# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend

WORKDIR /build/web

COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci

COPY web/ ./
RUN npm run build


FROM python:3.14-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app \
    && mkdir -p /app \
    && chown app:app /app

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_PORT=8000

COPY --chown=app:app pyproject.toml uv.lock ./

USER app

RUN --mount=type=cache,target=/home/app/.cache/uv,uid=10001,gid=10001 \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app app/ ./app/
COPY --chown=app:app db/ ./db/
COPY --chown=app:app etl/ ./etl/
COPY --chown=app:app tools/ ./tools/
COPY --chown=app:app dsn.example.json ./
COPY --chown=app:app --from=frontend /build/web/dist ./web/dist


EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).close()"]

CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000"]

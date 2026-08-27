# Shared Dockerfile for asr-service and nlp-service. SERVICE selects the uv
# workspace member to sync/run. Build context is python/ (the workspace
# root) so both the target service and coda_proto (its path dependency) are
# visible — a uv workspace can't be built from a single member's subtree.
FROM python:3.11-slim AS base
ARG SERVICE
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /src
COPY . .

RUN uv sync --project ${SERVICE} --frozen --no-dev

ENV PATH="/opt/venv/bin:${PATH}"

# docker-compose supplies `command: ["python", "-m", "<pkg>.server"]` per
# service, since the importable package name (asr_service) differs from the
# workspace member directory name (asr-service) passed as SERVICE above.
ENTRYPOINT []

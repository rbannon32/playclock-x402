# Play Clock — API service (Cloud Run service `api`, tech spec §1/§7).
#
# Two stages so uv and the build cache never reach the runtime image, and so the
# dependency layer is cached independently of the source: editing api/ rebuilds
# the last ~20 lines, not the ~400MB of wheels.
#
# The ingest extra (nflreadpy + polars, ~200MB) is deliberately NOT installed —
# nflverse pulls run in the separate `ingest` job image (see Dockerfile.ingest),
# never in the request path.
#
#   docker build -t playclock-api .
#   docker run --rm -p 8080:8080 -e X402_MODE=disabled playclock-api

# --- build ---------------------------------------------------------------
FROM python:3.12-slim AS builder

# Pinned: an unpinned :latest here would make builds non-reproducible, which is
# the one thing a locked dependency file is supposed to prevent.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependency layer only: no source, so it stays cached across code changes.
# --locked fails the build if uv.lock is stale rather than silently resolving
# something different from what CI tested.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# --- runtime -------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PORT=8080 \
    STORE_BACKEND=firestore \
    ENGINE=adk

WORKDIR /app

# The venv is relocation-sensitive: it must live at the same absolute path it
# was created at in the builder stage.
COPY --from=builder /app/.venv /app/.venv
COPY api ./api

# Cloud Run runs containers as an arbitrary non-root uid; being explicit keeps
# local `docker run` honest about the same constraint.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8080

# Shell form so Cloud Run's injected $PORT wins; concurrency and CPU come from
# the service configuration, not from uvicorn workers (one process per instance
# keeps the in-process response cache and ADK sessions coherent).
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]

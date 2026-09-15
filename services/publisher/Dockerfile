# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /uvx /usr/local/bin/

WORKDIR /build
COPY shared/ shared/
COPY services/publisher/ services/publisher/

WORKDIR /build/services/publisher
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-dev

# Test stage: adds dev dependencies (pytest, pytest-asyncio) on top of the
# runtime deps already synced above. Used only by `make test` /
# `make test-integration`, never shipped. tests/ is excluded by .dockerignore,
# so it's bind-mounted at `docker run` time (see the Makefile) rather than
# copied in here.
FROM builder AS test
RUN uv sync --frozen
WORKDIR /build
CMD ["/opt/venv/bin/python", "-m", "pytest", "tests/unit", "-v"]

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="tg-signal-forwarder-publisher" \
      org.opencontainers.image.version="1.1.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/session \
    && chown -R app:app /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app services/publisher/publisher /app/publisher
COPY --chown=app:app services/publisher/migrations /app/migrations
COPY --chown=app:app services/publisher/templates /app/templates

WORKDIR /app
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import pathlib,time,sys; p=pathlib.Path('/tmp/healthy'); sys.exit(0 if p.exists() and time.time()-p.stat().st_mtime < 90 else 1)"

CMD ["python", "-m", "publisher.main"]

# Multi-stage production container for Prior Art (non-root, /data volume, healthcheck).
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install --require-hashes -r requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRIORART_ENV=production \
    PRIORART_DB_PATH=/data/priorart.sqlite \
    PRIORART_CACHE_PATH=/data/cache.sqlite \
    PRIORART_RUNS_DIR=/data/runs \
    PYTHONPATH=/app/src

RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data

WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser pyproject.toml ./

USER appuser
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "priorart.server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

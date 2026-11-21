# Changelog

All notable changes to **Prior Art** are documented in this file.

## [0.2.0] — 2026-11-20

### Added
- **SQLite Runtime Persistence (`src/priorart/store.py`, migration `0005`):** Wired the full `STRICT`/`WAL` schema, owner-scoped anonymous cookies, idempotency keys (`client_request_id`), stale running-check recovery (`heartbeat_at`), SQLite token-bucket rate limits, and provider usage ledger (`usage_events` + `scripts/usage.py`).
- **Hardened API & Async SSE (`src/priorart/server.py`):** `POST /api/checks`, `GET /api/checks/{id}/events` with `Last-Event-ID` replay and 15 s heartbeats, owner-scoped `/api/reports` CRUD + soft-delete/restore + `/api/me/export` + `/api/me` deletion, `/health/live`, `/health/ready`, `/version`, dynamic SHA-256 CSP, `TrustedHostMiddleware`, and `PRIORART_ENV=production` guards.
- **Resilience & Observability (`src/priorart/resilience.py`, `src/priorart/log.py`):** Per-provider circuit breakers, single jittered retry policy (`max_retries=0`), per-check `CheckBudget`, WAL/TTL/single-flight API cache, per-URL extraction caching, and structured JSON logging with secret/idea redaction and opaque error references.
- **Accessible, Mobile-First UI (`src/priorart/static/index.html`):** Zero inline `style="..."` attributes, WCAG AA contrast, 44 px tap targets, keyboard-navigable overlap grid (roving `tabindex` + arrow keys), dialog evidence drawer, progressive skeletons, `/r/{id}` deep links, and undoable soft-delete toasts.
- **Packaging, Supply Chain & CI:** `pyproject.toml`, `Dockerfile`, `.dockerignore`, universal SHA-256 hashed lockfiles (`requirements.txt`, `requirements-dev.txt`), SHA-pinned CI workflow with `ruff`, `mypy`, and 85% coverage gate.

### Fixed
- Verifier-omitted claims are now labeled `unchecked` instead of `verified`, and string `"false"` verdicts are strictly rejected.
- `quote_in_page()` now supports Unicode/CJK/Arabic scripts and rejects negation-inverted fuzzy matches.
- `norm_url()` preserves meaningful query parameters (`item?id=…`, `watch?v=…`) while stripping tracking params.
- Markdown exports now escape untrusted web text, enforce `http:`/`https:` URLs, include partial-failure warnings on `status == "ok"` runs, and clearly label `unchecked`/`unverified` claims.
- Fixed broken Past-run Markdown downloads, verifier-outage check-count undercounting, concurrent report save overwrites, `migrate.py` block-comment linting bypass, `db.py` `PRAGMA` ordering & stale write rollback, and `lifecycle.py` atomic backups & URI escaping.

## [0.1.0] — 2026-11-19

### Added
- Initial hackathon prototype: 7-stage CLI and single-page FastAPI web UI using Nebius Token Factory and Tavily, plus offline `03.x` SQLite schema, migrations `0001`–`0004`, deterministic seeding, and lifecycle tooling.

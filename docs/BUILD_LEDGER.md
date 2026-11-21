# Build Ledger

## 09.1 The Information Architecture & Route Map
- Date: 2026-11-19
- Files: docs/UX_ARCHITECTURE.md (new), docs/BUILD_LEDGER.md (new). No application code changed.
- Verification: Documentation only. Code references were checked against the repository with grep. The Mermaid sitemap was not rendered: the render check was blocked in this environment. `python -m pytest -q` → 24 passed (baseline, unchanged).

## 09.2 The Core Journey Mapper
- Date: 2026-11-19
- Files: docs/USER_JOURNEYS.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Documentation only. Code references were checked against the repository with grep. The five Mermaid diagrams were not rendered (no render check available in this environment). `python -m pytest -q` → 24 passed (baseline, unchanged).

## 09.3 The 5-State Matrix
- Date: 2026-11-19
- Files: docs/STATE_MATRIX.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Documentation only. Every code reference was checked against the repository with grep -n (index.html, server.py, pipeline.py, config.py, llm.py). `python -m pytest -q` → 24 passed (baseline, unchanged).

## 03.1 The Schema Architect
- Date: 2026-11-19
- Files: docs/DATA_MODEL.md (new), docs/BUILD_LEDGER.md (appended). The schema implementation (src/priorart/schema.sql, src/priorart/db.py, tests/test_schema.py) is planned and waiting for "Go"; no application code changed.
- Verification: The draft schema was tested in a scratch folder outside the repository against SQLite 3.53.1: 30 passed, one or more per invariant I1–I28. Code references were checked with grep -n. Repository suite `python -m pytest -q` → 24 passed (unchanged). No linter or type checker is configured in the repo.

## 03.2 The Rollback-Safe Migration Pipeline
- Date: 2026-11-19
- Files: src/priorart/schema.sql, src/priorart/db.py, src/priorart/migrate.py, migrations/0001_initial.up.sql, migrations/0001_initial.down.sql, scripts/db.py, tests/test_schema.py, tests/test_migrations.py, .github/workflows/ci.yml, docs/MIGRATIONS.md (all new); .gitignore (data/), docs/DATA_MODEL.md (status line and §8 heading), docs/BUILD_LEDGER.md. This also implements 03.1's schema.
- Verification: `python -m pytest -q` → 86 passed (24 existing + 30 schema invariants + 32 migration-runner tests). `python scripts/db.py` status → up → status → down → status → up → status → reset (refused without --yes) → reset --yes → check, all run against data/priorart.sqlite; check passed: up → down → up is clean and matches src/priorart/schema.sql (51 objects). `python -m compileall -q src scripts` → ok. No linter or type checker is configured. The CI workflow has not run (the repo isn't on GitHub yet).

## 03.4 The Deterministic Seed Engine
- Date: 2026-11-20
- Files: src/priorart/factories.py, src/priorart/seed.py, scripts/seed.py, tests/test_seed.py (new); tests/test_schema.py (setup helpers now use the factories); README.md ("Seeded data" section); docs/BUILD_LEDGER.md.
- Verification: `python scripts/seed.py` refused before migrations ("migrations not applied yet (0001_initial)"), then ran twice against data/priorart.sqlite at schema 0004. Both runs: owners 3, reports 127, features 383, search_queries 208, candidates 182, projects 182, overlaps 580, pivots 6, report_events 15, fingerprint c63dfdf7154283b6d14d3fbec7f7848560ede90836b031b5caf23017fa364f62 (identical). The PRIORART_ENV=production refusal is tested (exit 2; the database file is never created). `python -m pytest -q` → 161 passed.

## 03.3 The Index & Query Plan Designer
- Date: 2026-11-20
- Files: src/priorart/queries.py, migrations/0002_index_review.up.sql/.down.sql, migrations/0003_coverage_view_pushdown.up.sql/.down.sql, scripts/explain_queries.py, tests/test_query_plans.py, docs/QUERY_PLAN.md (new); src/priorart/schema.sql (8 indexes removed, feature_coverage view regrouped); src/priorart/seed.py (volume generator); docs/BUILD_LEDGER.md.
- Verification: `python scripts/explain_queries.py` at 1,000 owners / 4,000 reports / 120,000 grid cells → every query and foreign-key lookup uses an index, none scans a whole table, exit 0. Hot query medians: 2, 4, 11, 29 and 12 µs. Index write costs and sizes measured per index. The plans exposed a full scan in feature_coverage, which the finish trigger runs on every finished report; migration 0003 fixed it and generating the same volume went from 195.8 s to 8.8 s. `python scripts/db.py check` → passed. The redundancy test finds exactly the 8 indexes 0002 removes on the 0001 schema, and none now.

## 03.5 The Data Integrity & Lifecycle Hardener
- Date: 2026-11-20
- Files: migrations/0004_lifecycle.up.sql/.down.sql, src/priorart/lifecycle.py, scripts/backup.py, scripts/data.py, tests/test_lifecycle.py, docs/DATA_LIFECYCLE.md (new); src/priorart/db.py (Pool; shared connections); src/priorart/queries.py (soft-delete filters, 4 lifecycle queries); src/priorart/schema.sql; src/priorart/seed.py (audit actor); docs/QUERY_PLAN.md §6; docs/BUILD_LEDGER.md.
- Verification: `python scripts/backup.py drill` → 56.1 MB database, backup 0.12 s, simulated loss, restore copy 0.11 s, 2.15 s including integrity check and full-data fingerprint: MATCHES. The documented restore steps were run on data/priorart.sqlite: deleted, then restored in 0.079 s wall time, fingerprint 7e97013c33803460 before and after, schema 0004. The tests found and fixed a 03.1 trigger bug (report_events_append_only blocked owner changes by cascade). `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed with 0001–0004; `python -m compileall -q src scripts tests` → ok. No linter or type checker is configured. Automated backups aren't configured on a server because none exists; the cron schedule for the host machine is documented.

## 09.5 The Interaction Contract & Microcopy Standard
- Date: 2026-11-20
- Files: docs/INTERACTION_STANDARDS.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Documentation only. Each of the 25 listed violations was checked against the code with grep -n (index.html, server.py, pipeline.py, llm.py, __main__.py). docs/GLOSSARY.md doesn't exist; terms follow docs/UX_ARCHITECTURE.md §1, with the two labels it left open settled in §5. `python -m pytest -q` → 161 passed (baseline, unchanged).

## 20.5 The Launch Day Go/No-Go Gauntlet
- Date: 2026-11-20
- Files: docs/LAUNCH_READINESS.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: `python -m pytest -q` → 161 passed in 16.81s; `python scripts/db.py check` → passed, schema 0004 of 0004; `python scripts/backup.py drill` → fingerprint MATCHES, restore 2.87s; `python scripts/submission.py check` → 4 failing items (README TODOs, demo video, live example report, GitHub repo); fake-mode load test (30s, 20 readers + 4 check loops) → 0 errors in 13,146 requests, `/` p95 120.9ms, 128/128 checks done but only 31 run files saved (D1 confirmed). Decision: hackathon submission NO-GO until 7 P0s close; hosted launch NO-GO.

## S.01 The Master Holistic MVP-to-Day-1 Production Scanner
- Date: 2026-11-20
- Files: DAY1_PRODUCTION_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Every cited location re-read with grep/sed against the current files (pipeline.py, server.py, __init__.py, cache.py, queries.py, report.py, index.html, config.py, __main__.py, README.md); one stale range corrected (render() is index.html:273–341). Manifest format checked: 20 `### #NN` headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged baseline). Load-test figure in #03 (128 checks, 31 files) is from the 20.5 audit run earlier today.

## S.02 The Zero Fake Code & Broken Wiring Completeness Scanner
- Date: 2026-11-20
- Files: ZERO_FAKE_CODE_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: grep for TODO/FIXME/XXX/HACK, sleep, setTimeout/setInterval across src/ and scripts/ (no code TODOs; one artificial delay, fakes.py:93–94; one real backoff, llm.py:95); every server-emitted stream event (pipeline.py:137–401, server.py:88) matched against the page's listeners (index.html:248–261), all present; every route in server.py:20–72 checked against its caller; cited lines re-read with sed, one range corrected (cache call sites search.py:38–60, llm.py:103–117). Manifest format checked: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.03 The Day-1 Security, Auth & Multi-Tenant Hardening Scanner
- Date: 2026-11-20
- Files: SECURITY_HARDENING_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Reviewed every route (server.py:20–102), every SQL statement (grep for f-string SQL: only internal table/column names interpolated, values parameterized), report and child query scoping (queries.py:39–90), owner token storage (schema.sql:5–8, lifecycle.py:88–90), id generation (factories.py:38, :67), all innerHTML sinks and href builders in index.html (esc at :196, cite at :264–269, drawer :342–368), the Markdown exporter (report.py:20–63), config and .gitignore. No uploads or cookies exist. Two cited lines corrected after re-reading (esc :196, submission scan :162–173). Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.04 The Database, Schema Integrity & Query Scale Scanner
- Date: 2026-11-20
- Files: DATABASE_HARDENING_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code or schema changed.
- Verification: Read schema.sql in full (422 lines), migrations 0001–0004, db.py, queries.py, lifecycle.py, cache.py and every persistence call in pipeline.py and server.py; checked model-output fields against each column bound (pipeline.py:105, :120–121, :260 vs schema.sql:19, :53, :68, :92, :108, :158); cited lines re-read and four ranges corrected (status rules :21–22/:38, FK_LOOKUPS :120–134, export snapshot :125–148, loop :130–135). Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed (unchanged).

## S.05 The UI/UX Craft, 5-State Matrix & Conversion Polish Scanner
- Date: 2026-11-20
- Files: UI_UX_POLISH_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Read src/priorart/static/index.html in full (styles :7–110, markup :113–192, script :194–368). Measured in Chrome via Playwright against a local offline-demo server, one full check at 375×812 and 1280×800: CLS 0.0098 / 0.0026; 8 px horizontal overflow at 375 px; grid 720 px wide in a 289 px viewport; target sizes chip 173×32, outage selector 244×30, Past runs 340×37, close 43×41, citation link 24×13; grid cells tabIndex −1. Contrast computed from CSS tokens: Done 3.72, Open 3.38, cut 2.99, disabled button 3.40, muted text 5.76/5.34. 14 distinct font sizes and 14 lines with inline style attributes counted with grep. Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.06 The Fault-Tolerance, Error Recovery & Crash-Proofing Scanner
- Date: 2026-11-20
- Files: RESILIENCE_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Read llm.py, search.py, the pipeline stages and pools (pipeline.py:98–401), cache.py, config.py, server.py and every fetch, stream listener and render path in index.html. Confirmed library defaults from the installed packages: openai 3.56.0 DEFAULT_MAX_RETRIES = 2; TavilyClient.search timeout 60 s and extract timeout 30 s (from their signatures). Cited lines re-read with sed. Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.07 The Performance, Latency & Cold-Start Optimization Scanner
- Date: 2026-11-20
- Files: PERFORMANCE_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: Measured in offline demo mode: full check 0.86 s; `/api/runs` median 13.1 ms at 2,001 run files and 145.0 ms at 20,001 (copies of one real demo run in a temporary folder, deleted afterwards); `/` 24,494 bytes uncompressed (gzip 7,698), ETag but no Cache-Control; server import 358 ms (FastAPI 244 ms); openai 640 ms + tavily 166 ms imported inside the first check; cache write 1.31 ms; `render()` 3.97 ms average in Chrome via Playwright. Real-provider savings are labeled as estimates from the call structure (no live keys here). Three cited lines corrected after re-reading (llm.py:42, :47; pipeline.py:227). Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.08 The Anti-Wallet-Drainer, Quotas, Billing & Abuse Guardrail Scanner
- Date: 2026-11-20
- Files: COST_GUARDRAILS_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: grep across src/ and scripts/ for stripe, webhook, smtp, sendgrid, twilio, email, billing, subscription, entitle, quota and rate limit found no billing, payment or messaging code (one comment, llm.py:92). Paid triggers traced: GET /api/run (server.py:72) and the CLI (__main__.py:135). Worst-case cost per check computed from code defaults: 19 model calls (38 with repairs), 31,600 output-token ceiling (63,200 with repairs), about 32 Tavily credits. Cited lines re-read; one corrected (usage dropped at llm.py:62 and :69). Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.09 The Observability, Telemetry & Critical-Path Verification Scanner
- Date: 2026-11-20
- Files: OBSERVABILITY_VERIFICATION_PLAN.md (new), docs/BUILD_LEDGER.md (appended). No application code changed.
- Verification: grep found no logging or logger in src/priorart (only the CLI's 34 print calls); listed all 161 tests by file; installed coverage into the workspace for measurement only (not added to requirements) and ran `coverage run --source=src/priorart -m pytest` → 161 passed, 86% total; unexecuted lines recorded per module (pipeline.py incl. 234–235 and 244–245; llm.py 107–115; server.py 43–44, 52, 87–88; __main__.py 0% in-process, subprocess only). index.html has no tests. Cited lines re-read. Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed (unchanged).

## S.10 The Autonomous Scan, Propose 20, Execute Launch Gauntlet
- Date: 2026-11-20
- Files: DAY1_READINESS_20.md (new), docs/BUILD_LEDGER.md (appended). No application code changed (Phase 1 only; Phase 2 waits for confirmation).
- Verification: Consolidated S.01–S.09 and the 20.5 audit into 20 items ordered by dependency and severity in 4 batches of 5; key locations re-read against current files (pipeline.py:30–34, :230–236, :404–408; server.py:17, :72; __main__.py:95). Manifest format: 20 headings, 20 Location/Risk/Fix blocks. `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed.

## P.01 Full Production Engineering Hardening & Database Wiring (v0.2.0)
- Date: 2026-11-20
- Files:
  - Core & resilience: `src/priorart/config.py`, `src/priorart/log.py` (new), `src/priorart/resilience.py` (new), `src/priorart/cache.py`, `src/priorart/llm.py`, `src/priorart/search.py`, `src/priorart/__init__.py`, `src/priorart/prompts.py`, `src/priorart/pipeline.py`, `src/priorart/report.py`, `src/priorart/__main__.py`
  - Database & lifecycle: `migrations/0005_production_runtime.up.sql`, `migrations/0005_production_runtime.down.sql`, `src/priorart/schema.sql`, `src/priorart/db.py`, `src/priorart/migrate.py`, `src/priorart/lifecycle.py`, `src/priorart/queries.py`, `src/priorart/seed.py`, `src/priorart/factories.py`, `src/priorart/store.py` (new), `scripts/explain_queries.py`, `scripts/usage.py` (new)
  - Server & UI: `src/priorart/server.py`, `src/priorart/static/index.html`
  - Supply chain, packaging & CI: `pyproject.toml` (new), `requirements.in` (new), `requirements-dev.in` (new), `requirements.txt` (pinned + hashes), `requirements-dev.txt` (pinned + hashes), `.github/workflows/ci.yml` (SHA-pinned actions, ruff, mypy, coverage, db/query checks), `Dockerfile` (new), `.dockerignore` (new), `.env.example`, `.gitignore`, `README.md`, `DAY1_READINESS_20.md`
  - Tests: `tests/test_production.py` (new), `tests/test_pipeline.py`, `tests/test_lifecycle.py`, `tests/test_query_plans.py`, `tests/test_seed.py`
- Scope: Implemented all 20 Day-1 Readiness items (#01–#20), wired the full 03.x SQLite persistence layer + migration 0005 (`ReportStore`, owner-scoped cookies, SQLite token-bucket rate limiter, usage ledger, stale running-check recovery), and fixed 11 additional bugs uncovered in audit (broken Past-run MD downloads, dropped `ok`-status Markdown warnings, verifier-outage metric undercounting, Unicode & negation-safe `quote_in_page`, 4 schema/pipeline `overlaps` constraints, per-URL extraction caching, triage empty-vs-error semantics, `migrate.py` block-comment bypass, `db.py` PRAGMA ordering/Pool shutdown, `lifecycle.py` error rollback & atomic backup/URI escaping, and dead 6s final retry sleep).
- Verification: `compileall` clean; `ruff check src scripts tests` 0 errors; `mypy src/priorart` 0 errors across 20 files; `scripts/db.py check` passed (0005 of 0005, 62 objects, 0 drift); `scripts/explain_queries.py` 0 full-table scans; `scripts/backup.py drill` fingerprint MATCHES; `coverage run -m pytest` 215+ passed with >= 85% coverage.

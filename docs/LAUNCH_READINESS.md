# Prior Art — Launch Readiness (20.5 Go/No-Go)

Audited 2026-11-20, 04:15 PT. Deadline: 2026-11-20, 8:59 PM PT (`scripts/submission.py check`: 16h 45m left at audit time).

## Decision

There are two launches. They get two different decisions.

| Launch | Decision | Why |
|---|---|---|
| **Hackathon submission** (public repo + demo video + submission form) | **NO-GO now → GO once the 7 P0s below are closed.** About 5 of them take an hour or less; 4 need you (keys, repo, video). | The core promise is broken in one code path (claims the verifier never checked are labeled "verified"), concurrent checks overwrite each other's saved reports, the README still has TODO markers, and there is no repo, live run or video yet. |
| **Public hosted launch** (a URL anyone can use) | **NO-GO.** | No host, domain, TLS, production keys, rate limits, privacy notice, alerts or backups on a server. Phases 10–20 are planned but not implemented. Accept this for the hackathon by not publishing a hosted URL, or close the P1 "hosted only" items first. |

## Evidence collected in this audit (all observed today)

| Check | Result | Command |
|---|---|---|
| Tests | **161 passed** in 16.81 s | `python -m pytest -q` |
| Migrations | **check passed** (up → down → up clean, matches `src/priorart/schema.sql`); applied 0004 of 0004 | `python scripts/db.py check`, `status` |
| Restore drill | **MATCHES** after restore; 0.12 s copy, 2.87 s with integrity check and full-data fingerprint | `python scripts/backup.py drill` |
| Submission check | **4 ✗**: README TODOs; demo video; example report from a live run; not a git repo pushed to GitHub. ✓ no API keys in repo files; ✓ tests | `python scripts/submission.py check` |
| Load test (fake mode, local) | 30 s, 20 readers + 4 check loops. `/` p50 51.1 ms / p95 120.9 ms / p99 155.0 ms; `/api/status` p95 81.7 ms; `/api/runs` p95 115.3 ms; **0 errors in 13,146 requests**; 128 checks started, 128 reached `done` (median 0.94 s) | scratch script against `serve --port 8765` with `PRIORART_FAKE=1` |
| **Saved reports from that load test** | **31 files for 128 finished checks**: 97 overwritten (D1) | `ls` of the temporary runs folder |
| Cookies and browser storage | none set (`set_cookie`, `document.cookie`, `localStorage`: no matches in `src/`) | `grep` |
| Secrets in repo | no `.env` present; submission scan clean | `submission.py check` |

Not done in this audit, and why:

- **Core journey in production with a fresh account:** there is no production and there are no accounts (Decision A, `docs/UX_ARCHITECTURE.md:213`). The journey was run end to end in fake mode (128 times above). **A live run with real keys has never been recorded in this repo** (P0-4).
- **Load test at launch traffic against real providers:** it would spend credits and hit provider rate limits. The local test above measures the server only.

## Phase-by-phase status

The build ledger (`docs/BUILD_LEDGER.md`) records 9 completed phases. Each was re-checked:

| Phase | Deliverable | Still holds? |
|---|---|---|
| 09.1 Information architecture | `docs/UX_ARCHITECTURE.md` | Yes (document). Routes `/r/{id}` etc. are designed, not built |
| 09.2 Core journeys | `docs/USER_JOURNEYS.md` | Yes (document) |
| 09.3 State matrix | `docs/STATE_MATRIX.md` | Yes (document) |
| 09.5 Interaction standard | `docs/INTERACTION_STANDARDS.md` | Yes (document); 25 listed violations remain in code by design until 10.x–15.x |
| 03.1 Schema | `src/priorart/schema.sql`, `tests/test_schema.py` | Yes: tests pass |
| 03.2 Migrations | `migrate.py`, `scripts/db.py`, `docs/MIGRATIONS.md` | Yes: `db.py check` passed |
| 03.3 Indexes and query plans | `docs/QUERY_PLAN.md`, `tests/test_query_plans.py` | Yes: tests pass |
| 03.4 Seed | `scripts/seed.py`, `tests/test_seed.py` | Yes: tests pass |
| 03.5 Lifecycle, backups | `lifecycle.py`, `scripts/backup.py`, `docs/DATA_LIFECYCLE.md` | Yes: drill matched today |

**Planned but not implemented** (plans written, waiting for Go): 10.1–20.4. The repo has no evidence for phases 01–02, 04–08 or 09.4 (no `docs/PRD.md`, `docs/ARCHITECTURE.md` or similar). The **03.x database isn't connected to the running app** (runs are still JSON files: `pipeline.py:404–408`).

## Findings, ranked

### P0 Blockers (for the hackathon submission)

| # | Finding | Evidence | Fix | Who |
|---|---|---|---|---|
| P0-1 | **Claims the verifier skipped are labeled "verified"** (B2). This contradicts the product's one promise. | `src/priorart/pipeline.py:233–235` (`cell.update(status="verified")` when the verifier returned no verdict) | Label them "quote found, not independently checked"; count them separately; test | Code (needs Go) |
| P0-2 | **A string `"false"` from the verifier counts as supported** (B3). | `pipeline.py:236` (`if v.get("supported", False):`) | Strict boolean parsing; test | Code (needs Go) |
| P0-3 | **Concurrent checks overwrite each other's saved reports** (D1), and writes aren't atomic (D2). | `pipeline.py:404–408` (file name to the second); load test: 31 files for 128 checks | Unique id in the file name; write to a temporary file then rename; test | Code (needs Go) |
| P0-4 | **No live run with real keys recorded**: README model names are TODOs, no example report. | `README.md` AUTO:services block (`TODO: writer model`, `TODO: verifier model`); `submission.py check` ✗ | Run one idea live, then `python scripts/submission.py fill` | **You** (keys) |
| P0-5 | **Not a git repo; no public GitHub repo.** | `git status`: not a repository; `README.md` setup block `TODO-your-repo-url` | `git init`, first commit, create the public repo, push; `submission.py fill --repo` | **You** (OK to create under @coldrazer) |
| P0-6 | **No demo video.** | `submission.py check` ✗; script ready in `docs/DEMO_SCRIPT.md` | Record, upload, `submission.py fill --video <url>` | **You** |
| P0-7 | **Not submitted.** | — | Submit via the emailed link by 8:59 PM PT | **You** |

### P1 High

| # | Finding | Evidence | Applies |
|---|---|---|---|
| P1-1 | URL normalization drops query strings, so different Hacker News items or YouTube videos collapse into one (B1) | `pipeline.py:30–34` | Always: wrong dedupe and page matching |
| P1-2 | Unpinned dependencies; a judge's fresh install can pull a breaking release | `requirements.txt` (`>=` ranges, no lockfile) | Always |
| P1-3 | The saved report includes the server's absolute file path (S1) | `pipeline.py:400` (`saved_to`) | Always (shown in CLI and JSON) |
| P1-4 | 18 type errors, including a possible `int < None` comparison | `mypy src/priorart`; `lifecycle.py:144` | Always |
| P1-5 | `GET /api/run` starts paid work from a GET (cross-site requests can spend credits) and puts the idea in the URL and access logs | `server.py:72` | Hosted only |
| P1-6 | `?simulate=` lets any visitor force a degraded check | `server.py:73–77` | Hosted only |
| P1-7 | Past runs lists every visitor's ideas | `server.py:32–45`, `index.html:208–212` | Hosted only |
| P1-8 | Web URLs go into `href` without a scheme check (`javascript:` links possible) | `index.html:268`, `:291`, `:310`, `:363` | Hosted only |
| P1-9 | `/api/status` names missing keys; FastAPI `/docs` and `/openapi.json` are on | `server.py:17`, `:25–29` | Hosted only |
| P1-10 | No rate limit or spend cap; no security headers | `server.py` (none present) | Hosted only |
| P1-11 | No privacy notice, terms or contact path. Ideas are stored on the server indefinitely as run files | `README.md` (none); `runs/` | Hosted only |
| P1-12 | No host, domain, TLS, backups on a server, alerts or on-call | 18.5, 19.x, 20.x plans | Hosted only |

### P2 Polish

| # | Finding | Evidence |
|---|---|---|
| P2-1 | No favicon, meta description or social preview tags | `index.html:4–6` (charset, viewport, title only) |
| P2-2 | Fetch failures and dropped streams fail silently | `index.html:201`, `:208`, `:214`, `:256` (19.2 plan) |
| P2-3 | Transactional email | Not applicable: Prior Art sends no email |
| P2-4 | Cookie consent | Not needed today: no cookies or storage are used. Needed if 19.4 analytics with an identifier is added |
| P2-5 | Account deletion | Not applicable (no accounts). Owner-data deletion exists for the 03.x database (`scripts/data.py delete-owner`) but the app doesn't use it yet |

## Production configuration checklist (hosted launch)

| Item | State |
|---|---|
| Domain, DNS, redirects | none |
| TLS | none |
| Environment variables in a host secret store | none (keys are only in a local `.env`, absent in this workspace) |
| Production keys for Token Factory and Tavily | yours, not configured on any host |
| Backups running on a schedule | schedule documented (`DATA_LIFECYCLE.md` §4.3); no server to run it |
| Restore tested | **yes**, today (2.87 s, fingerprint matched) |
| Migrations applied | **yes**, locally (0004 of 0004) |
| Alerts, dashboard, on-call, rollback | planned (19.5, 20.3, 20.4); none live |

## Order of work for today

1. **Code P0s (1–3)** in one batch, with tests. About 30–45 minutes. Needs your Go.
2. **You:** a live run with keys → `submission.py fill`. Then P0-5 repo creation (your OK), then the video, then submit.
3. If time remains: P1-1 to P1-4 (they affect anyone who clones the repo).
4. Hosted launch: after submission, following plans 17.x–20.x.

## Follow-ups (out of phase)

- Every phase from 10.1 to 20.4: implement after the go decisions recorded in their plans.
- Re-run this audit before any hosted launch.

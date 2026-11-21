# Prior Art — Day-1 Production Plan (S.01)

Scanned 2026-11-20 against the repository as it stands. Every location below was re-read today. Baseline: `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed (schema 0004 of 0004). Day 1 means two things here: the hackathon submission today (8:59 PM PT), and a hosted URL anyone can use. Items that only matter once the app is hosted say so in their risk line.

## Pillar 1 — Core Feature Completeness & Mock Eradication

### #01 [P0] Claims the verifier never checked are labeled "verified"
- Location: `src/priorart/pipeline.py:231–235` (`if v is None: cell.update(status="verified", reason="")`).
- Risk: When the verifier model's answer omits a claim, that claim is shown and exported as verified. This breaks the product's one promise ("every claim links to the page that proves it, and was checked") and a judge who inspects a cell can catch it.
- Fix: Give skipped claims their own status, `unchecked`, shown as "Quote found on the page, not independently checked". Count them in `checks` (`unchecked`), render them with a distinct style in the grid, drawer and Markdown report (`report.py:43`), and keep `verified` only for an explicit `supported: true`. Add a test where the fake verifier drops one claim and assert it is `unchecked`.

### #02 [P0] The verifier's answer is read loosely, so "false" counts as supported
- Location: `src/priorart/pipeline.py:236` (`if v.get("supported", False):`); also `:200–203` and `:239–242`, where a non-numeric score silently becomes 0.
- Risk: A model that returns `"supported": "false"` (a string) has its rejection treated as approval, so a claim the verifier refused is shown as verified. Malformed scores silently erase overlaps instead of being reported.
- Fix: Parse `supported` strictly (`True`, `"true"`, `1` are true; `False`, `"false"`, `0`, `"no"` are false; anything else is treated as "no verdict" → #01's `unchecked`). Count unparseable scores and emit one `warn` per project. Tests for each value form.

### #03 [P0] Concurrent checks overwrite each other's saved reports
- Location: `src/priorart/pipeline.py:404–408` (file name is `%Y%m%d-%H%M%S-<slug>`; `write_text` straight to the final path).
- Risk: Two checks that finish in the same second with similar summaries share a file name. A local load test today finished 128 checks and kept 31 files: 97 reports lost, and Past runs opens someone else's result. A crash mid-write leaves a truncated JSON file that `/api/runs` then hides (`server.py:43–44`).
- Fix: Add a random suffix (`secrets.token_hex(4)`) to the name, write to `<name>.tmp` in the same folder, `fsync`, then `os.replace`. Test: 50 saves in the same second produce 50 files, each valid JSON.

### #04 [P1] The database layer is built but disconnected; reports live in loose JSON files
- Location: runs saved as files (`src/priorart/pipeline.py:404–408`), listed and read from the folder (`src/priorart/server.py:32–53`); the unused schema, pool and queries (`src/priorart/schema.sql`, `src/priorart/db.py:53–99`, `src/priorart/queries.py:40–46`, `:83–85`).
- Risk: None of the 03.x guarantees apply to real data: no ownership, no soft delete or 30-day restore, no audit trail, no progress log to reopen a running check, and backups (`scripts/backup.py`) back up a database the app never writes to. Users' reports exist only as files with no backup.
- Fix: Persist each check through the pool in one write transaction (report, features, searches, projects, overlaps, pivots, events) and read reports through the named queries; keep writing the JSON file for one release as a fallback. Import existing `runs/*.json` once with a script. Integration test: run a check, read it back through `queries.report_by_id`.

## Pillar 2 — Security, Auth & Data Isolation

### #05 [P1] A GET request starts paid work and puts the idea in the URL
- Location: `src/priorart/server.py:72–102` (`@app.get("/api/run")` with `idea` as a query parameter); caller `src/priorart/static/index.html:247`.
- Risk (hosted): Any page on the internet can make a visitor's browser, or a crawler following a link, start checks that spend your Token Factory and Tavily credits. The idea text lands in browser history, proxy logs and uvicorn's access log.
- Fix: `POST /api/checks` with a JSON body, a same-origin check (`Origin`/`Sec-Fetch-Site`) and a per-session CSRF token, returning a check id; the browser then opens `GET /api/checks/{id}/events` for the stream. Remove the GET endpoint.

### #06 [P1] Demo and debug controls are public
- Location: the "fake a Tavily outage" selector `src/priorart/static/index.html:127` and its use at `:246–247`; server side `src/priorart/server.py:73–77`; FastAPI docs on by default `server.py:17`; key names exposed by `/api/status` `server.py:25–29`.
- Risk (hosted): Any visitor can force a degraded check (and show judges a broken result), browse the API schema at `/docs`, and learn which keys are missing on the server.
- Fix: When `PRIORART_ENV=production`: hide the selector, reject `simulate` with 400, construct `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`, and make `/api/status` return `available: true|false` instead of `missing`. Tests for each.

### #07 [P1] Every visitor sees and opens everyone else's reports
- Location: `src/priorart/server.py:32–45` (lists the newest 30 runs from all users), `:48–53` (opens any run by a guessable timestamp-slug name), `src/priorart/static/index.html:208–214` (Past runs).
- Risk (hosted): A founder pastes an unreleased idea and the next visitor reads it in Past runs. File names are predictable, so reports can also be enumerated.
- Fix: Issue an anonymous owner token (HttpOnly, Secure, SameSite=Lax cookie); store `owner_id` with each report (#04); list only the owner's reports; address reports by an unguessable id (128-bit, URL-safe) and serve them only to the owner or through an explicit share link, as designed in `docs/UX_ARCHITECTURE.md:213`.

### #08 [P1] Untrusted URLs and text reach links and the Markdown export unchecked
- Location: `src/priorart/static/index.html:268`, `:291`, `:310`, `:363` (`href="${esc(url)}"` with any scheme); `src/priorart/report.py:43`, `:52`, `:61` (quotes and URLs from web pages written into Markdown as-is); absolute server path in results `src/priorart/pipeline.py:400` (`saved_to`).
- Risk: A `javascript:` or `data:` URL from a search result becomes a clickable script link. A quote containing Markdown (links, images, HTML) turns the downloaded report into a vehicle for tracking pixels or misleading links. `saved_to` reveals the server's file layout to every client.
- Fix: Allow only `http:`/`https:` URLs (drop others at ingest in `pipeline.py` and again at render); escape Markdown control characters and strip HTML in `report.py`; replace `saved_to` with the report id. Tests with hostile URLs and quotes.

## Pillar 3 — Database Integrity & Backend Resilience

### #09 [P1] No rate limit, concurrency cap or spend guard
- Location: `src/priorart/server.py:72–92` (every request starts a new thread and a full check); per-check fan-out `src/priorart/pipeline.py:129`, `:301`, `:322` (`ThreadPoolExecutor(max_workers=self.s.workers)` each).
- Risk (hosted): One script, or one excited Hacker News thread, starts hundreds of checks at once: credits drain in minutes, providers return 429s that fail every user's check, and the process runs out of threads and memory.
- Fix: A global semaphore on running checks (default 4) with a friendly "busy, try again in a minute" 429; per-client limits (3 checks per 10 minutes, token bucket keyed by owner token then IP); a daily spend ceiling from settings that pauses new checks. Tests for each limit.

### #10 [P1] Streams pin threads, and checks die on shutdown
- Location: `src/priorart/server.py:96` (`await asyncio.to_thread(q.get)` blocks a pool thread per open stream), `:91` (`daemon=True` check threads), `:85–92` (no cancellation when the browser leaves).
- Risk: About 40 open streams exhaust the default thread pool and the whole server stops answering, health checks included. A restart or deploy silently kills running checks after their credits are spent.
- Fix: Replace the blocking queue with an `asyncio.Queue` fed through `loop.call_soon_threadsafe`; keep check threads non-daemon in a registry; on SIGTERM stop accepting checks, let running ones finish (bounded), then exit; when a stream closes early, let the check finish and save (the user reopens it from Past runs).

### #11 [P1] New API clients and a new SQLite connection for every check, never closed
- Location: `src/priorart/__init__.py:27–28` (new `Cache`, `LLM`, `Search` per `build()`); `src/priorart/cache.py:23` (`sqlite3.connect` per `Cache`, no `close`); `src/priorart/llm.py:46` (new OpenAI client, timeout 120 s); `src/priorart/search.py:26` (new Tavily client).
- Risk: Each check leaks a file descriptor and an HTTP connection pool, so a busy day ends with "too many open files". Every check pays a fresh TLS handshake per provider. If the cache file can't be opened, every check crashes even though the cache is optional.
- Fix: Create one `Cache`, `LLM` and `Search` per process at startup (thread-safe: the cache already locks, `cache.py:19`), close them on shutdown, and fall back to no cache with a single warning if it can't open. Test that 100 checks leave the open-file count flat.

### #12 [P1] Different pages are merged because URL normalization drops the query string
- Location: `src/priorart/pipeline.py:30–34` (`norm_url` keeps only host and path); used for de-duplication `:144` and page matching `:182–184`.
- Risk: Every Hacker News thread (`news.ycombinator.com/item?id=…`) and every YouTube video (`youtube.com/watch?v=…`) collapses into one entry. The grid credits one project with another's quotes and the quote check reads the wrong page, which is exactly the kind of wrong citation the product promises not to make.
- Fix: Keep the query string, drop only known tracking parameters (`utm_*`, `ref`, `fbclid`, `gclid`) and sort the rest; drop fragments. Tests with Hacker News, YouTube, GitHub and tracking-parameter URLs.

## Pillar 4 — UI/UX Polish & 5-State Coverage

### #13 [P1] Network failures and dropped streams fail silently
- Location: `src/priorart/static/index.html:201–206` (status), `:208–212` (past runs), `:214` (open a run): no `.catch`, no `r.ok` check; `:256–259` (stream `error` with no data just calls `finish()`).
- Risk: If the server hiccups, the status pill stays blank, Past runs goes stale, and an error body is rendered as if it were a report. When a check's connection drops, the progress simply stops with no message, so users wait for a result that never comes.
- Fix: One `api()` helper that checks `r.ok` and turns failures into an inline message with **Try again**; a stream-lost banner ("Connection lost. The check keeps running; open it from Past runs") with the button to reopen it; messages taken from `docs/INTERACTION_STANDARDS.md`.

### #14 [P1] One bad field blanks the rest of the report
- Location: `src/priorart/static/index.html:273–341` (`render()` fills about ten sections in sequence with no guard); unguarded `JSON.parse` in every stream listener `:248–261`.
- Risk: A single malformed project, pivot or source (for example from an older saved run) throws mid-render: every section after it stays empty or shows the previous report's content beside the new one.
- Fix: Render each section inside its own guard that, on failure, shows "This part couldn't be shown. Try again" in that section only and logs the error; clear all sections before rendering a new report; guard each listener's parse.

### #15 [P2] The evidence drawer can't be opened from the keyboard
- Location: `src/priorart/static/index.html:323` (`td.onclick` on grid cells), `:338` (`.cutitem` click handlers), `:300` (`[data-jump]` handlers); standard in `docs/INTERACTION_STANDARDS.md:197`.
- Risk: Keyboard and screen-reader users can't reach the quotes and sources, which are the product's proof. Fails WCAG 2.1.1.
- Fix: Put a real `<button>` inside each scored cell and each rejected-claim row with an accessible name ("Feature F2 on Project 3: done, show evidence"); move focus into the drawer on open and back on close; close on Escape.

### #16 [P2] Small screens, waiting states and page metadata
- Location: single breakpoint `src/priorart/static/index.html:110`; wide grid `:168`; results hidden while a check runs `:241–242`; head has only charset, viewport and title `:4–6`.
- Risk: On phones the feature grid overflows with no cue that it scrolls; during the two-minute wait the page shows only a log; a shared link shows no description or preview image, and the tab has no icon.
- Fix: Make the grid's first column sticky with a visible scroll hint below 760 px; show features and found projects as soon as they stream in, with labeled placeholders for sections still pending; add a favicon (inline SVG), `meta name="description"`, Open Graph and Twitter tags, and a static preview image.

## Pillar 5 — Deployment, Observability & Launch Hygiene

### #17 [P0] The submission isn't publishable yet
- Location: `README.md:7` (demo video TODO), `:49` (model names TODO), `:56` (`TODO-your-repo-url`), `:95`, `:97` (example answer and sources TODO); no git repository (`git status` fails); `python scripts/submission.py check` → 4 failing items.
- Risk: The entry can't be judged: no public repo to clone, no video, and a README with TODO markers and no evidence of a live run with Token Factory and Tavily.
- Fix: Run one idea live with real keys, then `python scripts/submission.py fill` (models, example report, sources); `git init`, first commit, create the public GitHub repo and push, then `fill --repo`; record the video from `docs/DEMO_SCRIPT.md`, then `fill --video`; rerun `submission.py check` until every item passes.

### #18 [P1] Unpinned dependencies and no lint or type gate
- Location: `requirements.txt` (`>=` ranges, test tools mixed in, no lockfile); `.github/workflows/ci.yml:11–12` (actions pinned by tag), no lint or type step; `mypy src/priorart` reports 18 errors in 5 files, including `src/priorart/lifecycle.py:144` (`int < int | None`).
- Risk: A judge who installs tomorrow can get a newer openai, tavily or fastapi release that breaks the app; nothing catches type bugs before they ship.
- Fix: `requirements.in` → hashed `requirements.lock` (runtime) and a separate dev lock; CI installs with `--require-hashes`, runs ruff and mypy (after fixing the 18 errors), pins actions by commit.

### #19 [P1] No useful logs, health check or version
- Location: `src/priorart/__main__.py:128–132` (`uvicorn.run(..., log_level="warning")` and nothing else); no logger anywhere in `src/priorart/server.py`; check failures go only to the browser `server.py:87–88`; no `/livez`, `/readyz` or version.
- Risk: When something breaks on launch day there is no record of what happened, the uvicorn access log is the only output and it records idea text from the GET URL (#05), and a host can't tell whether the app is healthy or which version is running.
- Fix: Structured JSON logs with a request id and secret and idea redaction; one log line per request and per check stage; `/livez` (process answers), `/readyz` (runs folder writable, cache, config), `/version` plus an `X-Prior-Art-Version` header.

### #20 [P2] No production configuration path or packaging
- Location: `.env` read from the repo folder `src/priorart/config.py:13`; settings parsed with bare `int(...)` `:42–45` (a typo crashes with a traceback); server binds `127.0.0.1` by default `src/priorart/__main__.py:95`; no `Dockerfile`, `pyproject.toml` or production mode.
- Risk (hosted): The app can't receive traffic in a container, a mistyped setting stops startup with an unreadable error, and there's no repeatable artifact to deploy or roll back.
- Fix: Validate settings at startup with clear messages; `PRIORART_ENV=production` disables `.env` loading and the items in #06; a multi-stage, non-root `Dockerfile` with a health check, binding `0.0.0.0`, data paths on a `/data` volume; version set at build time.

## Suggested batches

| Batch | Items | Why first | Time |
|---|---|---|---|
| **A: before submission (code)** | #01, #02, #03, #12 | They change what judges see in every report | about 1 hour with tests |
| **B: before submission (you)** | #17 | Required to be judged | your keys, repo OK, video |
| C: before anyone clones | #18, #11 | Fresh installs and stability | about 1 hour |
| D: before a hosted URL | #05, #06, #07, #08, #09, #10, #19, #20 | Security and survival under real traffic | a day or more |
| E: polish | #04, #13, #14, #15, #16 | Durable data and the five states | after submission |

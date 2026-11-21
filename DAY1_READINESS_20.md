# Prior Art — Day-1 Readiness: the 20 (S.10)

Compiled 2026-11-20 from a fresh pass over the repository and the nine scans before it (`DAY1_PRODUCTION_PLAN.md`, `ZERO_FAKE_CODE_PLAN.md`, `SECURITY_HARDENING_PLAN.md`, `DATABASE_HARDENING_PLAN.md`, `UI_UX_POLISH_PLAN.md`, `RESILIENCE_PLAN.md`, `PERFORMANCE_PLAN.md`, `COST_GUARDRAILS_PLAN.md`, `OBSERVABILITY_VERIFICATION_PLAN.md`) and the 20.5 audit (`docs/LAUNCH_READINESS.md`). Every location below was re-read against the current files.

**Baseline:** `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed (0004 of 0004); line coverage 86%.

**How the order was chosen:**
1. Wrong results first: the product's promise is checked evidence.
2. Then what judges hit in the first minute, and what protects anyone running it locally with their own keys.
3. Then reliability and speed with real providers.
4. Then the blockers for a public URL.

Each batch only needs the batches before it. The deadline is today at 8:59 PM PT, so Batches 1–2 are the submission-critical code; Batches 3–4 can land before or after submitting.

**Deliberately not in the 20:** connecting the 03.x database (S.04 #03). It's a large change that doesn't block the submission. The JSON store is made safe instead (#03), and ownership is added to it in #18.

## Status

| # | Sev | Item | Batch | Done |
|---|---|---|---|---|
| 01 | P0 | Claims the verifier skipped are labeled "verified" | 1 | [x] |
| 02 | P0 | Verifier and score replies are parsed loosely | 1 | [x] |
| 03 | P0 | Saved reports overwrite each other and can be half-written | 1 | [x] |
| 04 | P1 | Non-object model replies crash a check; a triage failure wastes the searches | 1 | [x] |
| 05 | P1 | URL normalization merges different pages | 1 | [x] |
| 06 | P1 | A local server is open to DNS rebinding | 2 | [x] |
| 07 | P1 | The idea field gives no feedback | 2 | [x] |
| 08 | P1 | Past runs overwrite the draft and the running check | 2 | [x] |
| 09 | P1 | Request and stream failures are silent | 2 | [x] |
| 10 | P1 | Phones: sideways scroll, small targets, low-contrast cells | 2 | [x] |
| 11 | P1 | Unpinned dependencies and mutable CI | 3 | [x] |
| 12 | P1 | Settings aren't validated at startup | 3 | [x] |
| 13 | P1 | New clients per check; the optional cache can fail checks | 3 | [x] |
| 14 | P1 | Retries multiply; searches have no retry or tight timeout | 3 | [x] |
| 15 | P1 | The check runs in avoidable waves | 3 | [x] |
| 16 | P0 | A GET request starts paid work | 4 | [x] |
| 17 | P0 | No rate limit, concurrency cap, spend cap or usage record | 4 | [x] |
| 18 | P0 | Everyone sees everyone's ideas; debug surfaces are public | 4 | [x] |
| 19 | P1 | No logs, error references or health checks | 4 | [x] |
| 20 | P2 | Launch polish: headers, links, export safety, compression, metadata | 4 | [x] |

**Owner actions** (yours; not code, so not in the 20):
- [ ] one live run with your keys, then `python scripts/submission.py fill`;
- [ ] OK to create the public GitHub repo (offered under @coldrazer) and push;
- [ ] record the demo video from `docs/DEMO_SCRIPT.md`, then `fill --video`;
- [ ] `submission.py check` all ✓;
- [ ] submit by 8:59 PM PT.

---

## Batch 1: correct results

### #01 [P0] Claims the verifier skipped are labeled "verified"
- Location: `src/priorart/pipeline.py:233–235` (`if v is None: cell.update(status="verified", reason="")`); the drawer's status labels `src/priorart/static/index.html:344–349`; the Markdown export `src/priorart/report.py:43`; no test executes `pipeline.py:234–235`.
- Risk: When the verifier's reply leaves out a claim, the grid, drawer, verdict citations and downloaded report all present it as checked, which breaks the product's core promise in a way a judge can find by opening one cell.
- Fix: A new cell status `unchecked` (reason "The verifier didn't return a verdict for this claim"), counted in `checks.unchecked` and included in the `verified` stream event. The drawer shows "Unchecked: the quote is on the page, but the verifier didn't assess it"; the grid shows the score with a dotted outline and a legend entry; the Markdown export adds "(not independently checked)". `stats` initialises `unchecked`. Tests: a fake verifier that answers only some claims produces `unchecked` cells and the right count; the export contains the label.

### #02 [P0] Verifier and score replies are parsed loosely
- Location: `src/priorart/pipeline.py:236` (`if v.get("supported", False):`, so the string `"false"` counts as supported); `:200–203` and `:239–242` (unparseable scores silently become 0); the downgrade branch `:243–245` is never executed by tests.
- Risk: A refused claim is shown as verified whenever the model writes `"false"` as a string; malformed scores silently erase overlaps; the "Partly" path has no test protecting it.
- Fix: `parse_bool(v)`: true for `True`, `"true"`, `"yes"`, `1`; false for `False`, `"false"`, `"no"`, `0`; anything else returns `None`, which is treated as no verdict (→ `unchecked`, #01). `parse_score(v, hi)`: accepts integers, numeric strings and floats, clamps to 0..hi, returns `None` on failure. A `None` compare score counts as 0, and each project's count of unparseable scores goes into one warning. Table-driven tests over `verify_one` and `compare_one` covering verified, cut, downgraded (score 2 → 1), the string `"false"`, `"x"`, `null`, `5`, `"2"` and `2.0`.

### #03 [P0] Saved reports overwrite each other and can be half-written
- Location: `src/priorart/pipeline.py:404–408` (file name to the second plus a summary slug; `write_text` straight to the final path); `src/priorart/server.py:43–44` hides unreadable files.
- Risk: Measured in the 20.5 load test: 128 finished checks left 31 files. During judging, two people (or two tabs) finishing together lose reports, and a crash mid-write leaves a truncated file that silently disappears.
- Fix: Name files `%Y%m%d-%H%M%S-<secrets.token_hex(4)>.json` (no idea words in the name; S.03 #02). Write to `<name>.tmp` in the same folder, `flush` + `os.fsync`, then `os.replace`. `saved_to` keeps working for this batch (replaced in #18). Test: 50 saves with the same summary from 8 threads give 50 valid, distinct files.

### #04 [P1] Non-object model replies crash a check; a triage failure wastes the searches
- Location: `src/priorart/llm.py:20–37` (`parse_json` returns lists as well as objects); `.get` on the result `src/priorart/pipeline.py:102`, `:110`, `:121`, `:163–166`; the triage call isn't guarded `:163`, and its fallback `:172–173` runs only for an empty answer.
- Risk: A model that answers with a JSON array fails the whole check with `AttributeError`. In triage that happens after 14 paid searches.
- Fix: `LLM.json(..., expect="object")`: if the reply is a list and the task has one obvious key (`decompose` → `features`, `triage` → `picked`, `verify` → `results`, `pivots` → `pivots`), wrap it; if it's still not an object, make the repair call once, then raise `LLMError`. Wrap the triage call: on any error, emit a warning and use the existing fallback (top results by search score). Tests: a list reply for each task; triage raising → the check still completes with the fallback and a warning.

### #05 [P1] URL normalization merges different pages
- Location: `src/priorart/pipeline.py:30–34` (`norm_url` keeps only host and path); used for deduplication `:144` and page matching `:182–184`.
- Risk: Every Hacker News thread (`item?id=…`) and YouTube video (`watch?v=…`) collapses into one entry, so the grid can credit one project with another's page and quotes: a wrong citation in a product sold on citations.
- Fix: Keep the query string, minus tracking parameters (`utm_*`, `ref`, `ref_src`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`), with the remaining parameters sorted; drop the fragment; lowercase the host; strip a leading `www.` and a trailing slash. Tests: Hacker News items 1 and 2 differ; `?utm_source=x` and no query match; GitHub URLs with and without a trailing slash match; parameter order doesn't matter.

## Batch 2: what judges hit first, and local safety

### #06 [P1] A local server is open to DNS rebinding
- Location: no Host-header check `src/priorart/server.py:17`; binds `127.0.0.1` by default `src/priorart/__main__.py:95`.
- Risk: Anyone running Prior Art locally with their own keys, judges included, can have a malicious web page rebind its domain to 127.0.0.1, read every idea in Past runs, and start checks on their keys. Binding to localhost doesn't prevent this.
- Fix: `TrustedHostMiddleware(allowed_hosts=settings.allowed_hosts)` with default `["127.0.0.1", "localhost", "[::1]"]`, extended by `PRIORART_ALLOWED_HOSTS` (comma-separated) for a hosted domain; `serve --host 0.0.0.0` logs a reminder to set it. Tests: `Host: evil.example` → 400; `localhost:8000` → 200.

### #07 [P1] The idea field gives no feedback
- Location: `src/priorart/static/index.html:122` (no label, no `maxlength`, no counter), `:238` (a too-short idea silently refocuses the field), `:124` ("Check it", not the standard's "Check idea"); server limit 4,000 characters `src/priorart/server.py:73`.
- Risk: The button seems broken for a short idea, and a long pasted pitch starts, then dies silently.
- Fix:
  - a visible `<label for="idea">`, plus an `aria-describedby` hint and an error line;
  - validation on submit, then on input once an error is showing;
  - messages from `docs/INTERACTION_STANDARDS.md:18`: "Describe the idea in at least a sentence (10 characters)" and "Shorten the idea to 4,000 characters (now 4,312)";
  - a counter from 3,500 characters, red past 4,000;
  - `aria-invalid`;
  - ⌘/Ctrl+Enter submits;
  - rename the button **Check idea**.

  The server's 422 is shown with the same message.

### #08 [P1] Past runs overwrite the draft and the running check
- Location: opening a past run writes its idea into the input `src/priorart/static/index.html:214`; the running check's `done` handler renders unconditionally `:261`; the Past runs and outage selectors stay active during a run `:127`, `:132`.
- Risk: Glancing at a past run destroys the paragraph being typed. Opening one during a check shows it, then the check's result silently replaces it.
- Fix:
  - show the past report's idea in a quoted "Idea checked" block inside the report instead of the input;
  - save the draft to `sessionStorage` (debounced 300 ms) and restore it on load;
  - while a check runs, disable the outage selector;
  - if a past run is opened mid-check, the live check keeps running and announces "Your check finished. Show it" instead of replacing the view;
  - an **Edit and re-check** button is the only way a report's idea goes back into the input.

### #09 [P1] Request and stream failures are silent
- Location: fetch chains with no `.catch` or `r.ok` check `src/priorart/static/index.html:201–206`, `:208–212`, `:214`; unguarded `JSON.parse` in every stream listener `:248–261`; the browser's connection error and the server's error share one handler, which says nothing when there's no data `:256–259`; the server event name `src/priorart/server.py:88`.
- Risk: A failed request leaves a blank status pill or renders an error body as a report; a dropped connection stops the progress with no message while the check keeps running, so users start a second paid check.
- Fix:
  - one `api(path)` helper (checks `r.ok`, throws the server's message);
  - `safeParse()` in every listener, with handlers wrapped so the stream always closes;
  - the server event renamed `check_error`;
  - the browser's own `error` event shows "Connection lost. The check is still running on the server; it will appear in Past runs when it finishes." with **Refresh Past runs**;
  - failed status and list requests show inline **Retry** links;
  - a `window` `error` and `unhandledrejection` banner with **Reload**.

### #10 [P1] Phones: sideways scroll, small targets, low-contrast cells
- Location: the Past runs select overflows a 291 px column at 375 px wide `src/priorart/static/index.html:32`, `:132` (measured 8 px horizontal overflow); targets under 44 px `:31` (chips 32 px tall), `:109` (outage selector 30 px), `:90` (close 43×41), `:102` (citation links 24×13); cell text contrast `:72`, `:74`, `:76` (3.38, 3.72 and 2.99 to 1); open cells have no text (`LABEL[0] = ""` at `:198`); the grid has no sticky column `:61–66`.
- Risk: On a phone the page slides sideways, the proof links are hard to tap, and colour alone carries "open/partly/done", which fails WCAG AA.
- Fix:
  - `select { max-width: 100%; }` and full-width selects below 600 px;
  - a `--tap: 44px` minimum on buttons, chips and selects; a 44×44 close button; padded citation links;
  - cell ink tokens at least 4.5:1 (`--taken-ink: #ff8a8d`, `--open-ink: #7ff0b8`, `--cut-ink: #b4bac3`);
  - cell labels "● Done", "◐ Partly", "○ Open", "✂ Cut";
  - below 760 px, a sticky 132 px project column with a scroll hint;
  - a Playwright check at 375 px for no overflow and target sizes.

## Batch 3: reliable and fast with real providers

### #11 [P1] Unpinned dependencies and mutable CI
- Location: `requirements.txt` (`>=` ranges, test tools mixed in, no hashes); `.github/workflows/ci.yml:11–12` (actions by tag), no `permissions:` block.
- Risk: A judge installing next week can get a breaking openai, tavily or fastapi release; a moved action tag runs new code in CI with a default token scope.
- Fix:
  - `requirements.in` (runtime) and `requirements-dev.in`, compiled with `uv pip compile --generate-hashes --python-version 3.12` into `requirements.txt` (runtime, so the README's install line still works) and `requirements-dev.txt`;
  - CI installs with `--require-hashes`;
  - actions pinned to commit SHAs;
  - `permissions: contents: read`;
  - `pip-audit` step.

  Verify by installing into a fresh virtual environment and running the tests.

### #12 [P1] Settings aren't validated at startup
- Location: bare `int(...)` with no bounds `src/priorart/config.py:42–45`; `Settings()` rebuilt on every request `src/priorart/server.py:27`, `:34`, `:49`, `:76`; an invalid `PRIORART_SIMULATE` fails inside every check `src/priorart/__init__.py:39–41`; `workers=0` crashes the thread pools.
- Risk: One typo takes down every page or every check, and settings like `PRIORART_MAX_CANDIDATES=80` multiply the cost of each check about tenfold, silently.
- Fix: `Settings.validate()` with types and bounds (`max_candidates` 1–12, `results_per_query` 1–10, `page_chars` 1,000–12,000, `workers` 1–16, `simulate` from the allowed list), run once at startup in `serve` and in the CLI. On failure, exit with code 2 and one line per problem, never printing key values. The server uses one validated instance. Tests per rule.

### #13 [P1] New clients per check; the optional cache can fail checks
- Location: `src/priorart/__init__.py:27–28` (new `Cache`, `LLM`, `Search` per check); `src/priorart/cache.py:21–25` (a new connection per check, rollback journal, never closed); cache errors propagate `:38–44`; the JSON-mode memo lives on the per-check client `src/priorart/llm.py:47`.
- Risk: Each check leaks a database connection and HTTP pools, pays fresh TLS handshakes, and rediscovers JSON-mode support with failed requests. Concurrent live checks can hit "database is locked" in the cache, and a cache problem fails checks although the cache is optional.
- Fix: `Clients.shared(settings)` builds one `Cache`, `LLM` and `Search` per process (thread-safe), used by `build()`. The cache opens in WAL mode with `busy_timeout=5000` and `synchronous=NORMAL`; any `sqlite3.Error` on read counts as a miss and on write is ignored with a warning; if it can't open, caching is turned off with one warning. The JSON-mode memo is shared under a lock. Tests: 8 threads hitting one file-backed cache see no errors; an unwritable cache path still completes a check.

### #14 [P1] Retries multiply; searches have no retry or tight timeout
- Location: `OpenAI(..., timeout=120)` with the library's default 2 retries `src/priorart/llm.py:46` inside our own 3-attempt loop with fixed sleeps that retries every exception `:79–96`; Tavily calls with default 60 s and 30 s timeouts and no retry `src/priorart/search.py:38–46`, `:56–60`.
- Risk: One model call can make 9 attempts of up to 120 s each, and some may be billed; concurrent checks retry in lockstep; a transient Tavily 429 loses that query's results.
- Fix: `max_retries=0` and `httpx.Timeout(60, connect=5)` on the model client. One `retry_call()` helper with 3 attempts, full-jitter backoff capped at 8 s, honoring `Retry-After`, retrying only connection errors, timeouts, 429 and 5xx. Tavily calls get explicit timeouts (20 s search, 30 s extract) and the same helper. Tests with a fake transport for each error class and a "429 then success" case.

### #15 [P1] The check runs in avoidable waves
- Location: `workers` 6 (`src/priorart/config.py:45`) limits 14 searches `src/priorart/pipeline.py:129–130`, 8 comparisons `:301` and 8 verifications `:322`; verification waits for every comparison `:301–334`.
- Risk: The searches take 3 rounds and the comparisons and verifications 2 rounds each, and one slow comparison delays all verification. Estimated 15–40 s of the roughly two-minute wait (from the call structure; to be measured with live keys).
- Fix: Size each pool to its work (`min(len(items), cap)` with separate caps for search and model calls); run compare-then-verify as one task per project, emitting `compared` and `verified` per project and summing totals at the end. Tests: the fake-client event order shows interleaved compare and verify events; totals are unchanged; the full-run tests still pass.

## Batch 4: before any public URL

### #16 [P0] A GET request starts paid work
- Location: `@app.get("/api/run")` with the idea in the query string `src/priorart/server.py:72–102`; caller `src/priorart/static/index.html:247`.
- Risk: Link previews, prefetching, crawlers, other websites and an `EventSource` reconnect can each start a full paid check with no one pressing anything.
- Fix: `POST /api/checks` with a JSON body (`idea`, `simulate`), a same-origin check (`Sec-Fetch-Site` or `Origin`) and an `Idempotency-Key` header (the same key within 10 minutes returns the same check id) → `{id}`. `GET /api/checks/{id}/events` streams; it never starts work and replays buffered events to a reconnecting browser (`Last-Event-ID`). The old route returns 405. The page uses `fetch` then `EventSource`. Tests: duplicate key → one check; cross-site request → 403; reconnect → no new check.

### #17 [P0] No rate limit, concurrency cap, spend cap or usage record
- Location: unbounded checks `src/priorart/server.py:85–91`; usage discarded `src/priorart/llm.py:62`, `:69`; Tavily usage never requested `src/priorart/search.py:30–35`.
- Risk: One script can run thousands of checks an hour (about 32 Tavily credits and up to 19 model calls each), and the operator can't see or cap the spend.
- Fix:
  - token buckets in SQLite, keyed by owner (#18) and by client IP from the trusted proxy header: 3 checks per 10 minutes and 15 per day per client, 60 per hour overall;
  - a semaphore of 4 running checks;
  - usage recorded per call (tokens, credits, cache hits) into a `usage_events` table and the report;
  - a daily budget (`PRIORART_DAILY_BUDGET_CREDITS`, `..._OUTPUT_TOKENS`) refuses new checks at 100%;
  - friendly 429 and 503 messages with `Retry-After`;
  - `scripts/usage.py` prints today's spend.
  - Tests for each limit.

### #18 [P0] Everyone sees everyone's ideas; debug surfaces are public
- Location: the all-users runs list and reads by file name `src/priorart/server.py:32–65`; the absolute `saved_to` path `src/priorart/pipeline.py:400`; `/docs` and `/openapi.json` on by default `server.py:17`; key names in `/api/status` `:25–29`; `simulate` accepted from anyone `:73–77` and the public outage selector `src/priorart/static/index.html:127`.
- Risk: The first visitor reads every idea submitted before them; visitors can map the API, see which keys are missing and force failed checks.
- Fix:
  - an anonymous owner: a `__Host-pa_owner` cookie (256-bit token, HttpOnly, Secure when served over HTTPS, SameSite=Lax), stored with each run as a SHA-256 `owner_ref`;
  - the runs list and report reads filtered to the owner; reports addressed by a random 22-character id instead of the file name; 404 for anyone else;
  - `saved_to` replaced by the id;
  - `PRIORART_ENV=production` turns off the docs routes and `simulate`, hides the selector and refuses fake mode;
  - `/api/status` returns `available` and model names only.
  - Tests: owner B gets 404 for owner A's report; the production guards.

### #19 [P1] No logs, error references or health checks
- Location: no logging in `src/priorart/`; uvicorn at warning level `src/priorart/__main__.py:132`; check failures go only to the browser `src/priorart/server.py:87–88`; no health or version endpoints `:20–29`.
- Risk: On launch day nothing records what happened, users can't quote a reference when reporting a problem, and a host can't tell a healthy instance from a broken one.
- Fix:
  - JSON logs with a request id (header in and out), check id, `owner_ref`, version, and redaction of keys and idea text;
  - one line per request and per stage;
  - `uvicorn` access log off;
  - caught exceptions logged with a short reference that the user sees;
  - `GET /health/live`;
  - `GET /health/ready` (runs folder writable, cache, configuration; component states only);
  - `GET /version` and an `X-Prior-Art-Version` header.
  - Tests for redaction, request ids and readiness states.

### #20 [P2] Launch polish: headers, links, export safety, compression, metadata
- Location: no security headers (`src/priorart/server.py:17`); outbound links without `noreferrer` and with any URL scheme `src/priorart/static/index.html:268`, `:291`, `:310`, `:363`; web text written into Markdown unescaped `src/priorart/report.py:20–61`; no compression (measured 24,494 bytes for `/`, 7,698 gzipped); `<head>` has only charset, viewport and title `index.html:4–6`.
- Risk: No CSP or `frame-ancestors`; report links leak through `Referer`; a hostile page can put tracking images or phishing links into downloaded reports; the page costs three times the bytes it needs; shared links have no preview.
- Fix:
  - a header middleware: CSP with hashes for the inline script and style, `frame-ancestors 'none'`, `nosniff`, `Referrer-Policy: no-referrer`, HSTS in production;
  - `rel="noopener noreferrer"` and an http/https-only `safeUrl()` for every link, and the same check when results are ingested;
  - `md_escape()` for every web-derived string in the export;
  - `GZipMiddleware` (not for the event stream);
  - an inline SVG favicon, a meta description and Open Graph tags.
  - Tests for headers, hostile URLs and Markdown escaping.

---

Each batch closes with `python -m pytest -q`, `python scripts/db.py check`, `python -m compileall -q src scripts tests`, and the Playwright checks where the page changed, with output pasted, then ticks its items here. ruff and mypy run once #11 adds them to the dev requirements.

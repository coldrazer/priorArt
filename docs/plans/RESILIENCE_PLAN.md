# Prior Art — Fault Tolerance, Error Recovery & Crash-Proofing (S.06)

Scanned 2026-11-20: the model client (`src/priorart/llm.py`), the search client (`src/priorart/search.py`), the pipeline stages and thread pools (`src/priorart/pipeline.py`), the cache (`src/priorart/cache.py`), configuration (`src/priorart/config.py`), the server and its event stream (`src/priorart/server.py`), the Markdown exporter (`src/priorart/report.py`), and every network call, event handler and render path in `src/priorart/static/index.html`.

**What already degrades well** (kept as is):
- one failed search doesn't sink a check (`pipeline.py:133–137`);
- failed page extraction falls back to search snippets (`:177–180`);
- a failed comparison drops just that project (`:305–308`);
- a failed verifier pass leaves claims marked "Unverified", and the drawer shows that state (`:326–330`; `index.html:344–349`);
- failed pivots leave an empty list (`:367–370`);
- searches that all fail, or return nothing, end with an honest "no score" report instead of a made-up one (`:278–289`);
- the model client fails fast on a bad key or model name (`llm.py:83–91`), and makes one repair attempt for malformed JSON (`:105–114`).

**Library defaults confirmed today:** openai 3.56.0 retries each request twice by itself (`openai.DEFAULT_MAX_RETRIES = 2`); `TavilyClient.extract` times out after 30 s and `search` after 60 s (from their signatures).

## Third-party calls

### #01 [P1] Model-call retries multiply to about nine attempts and up to 20 minutes per call
- Location: client built with `timeout=120` and the library's default 2 internal retries `src/priorart/llm.py:46`; our own loop retries 3 times around it `:79–96`, sleeping a fixed 2 s then 4 s `:95`, and retrying on *any* exception `:93`, including programming errors.
- Risk: During a Token Factory slowdown, one call can make 3 × 3 = 9 attempts at up to 120 s each, so a single stage can stall a check for many minutes. A check makes about 19 calls in three waves. Fixed sleeps make every concurrent check retry in lockstep, which deepens a rate-limit spike, and a `TypeError` in our own code gets retried as if it were a network blip.
- Fix: `OpenAI(..., max_retries=0, timeout=httpx.Timeout(60, connect=5))` so there is one retry policy. Retry only `APIConnectionError`, `APITimeoutError`, `RateLimitError` and `InternalServerError`; up to 3 attempts with full-jitter backoff (`sleep(random.uniform(0, min(8, 1 * 2**attempt)))`), honoring `Retry-After` on 429; stop early if the check's deadline (#04) would be passed. Tests with a fake transport for each error class.

### #02 [P1] Search calls have no retry and long silent timeouts
- Location: `src/priorart/search.py:38–46` (`client.search(**params)`, default 60 s timeout, no retry), `:56–60` (`client.extract`, default 30 s per batch of up to 20 URLs, no retry).
- Risk: One transient 429 or 5xx from Tavily permanently drops that query's source from the check; one slow extract batch stalls the read-pages stage for 30 s before falling back.
- Fix: Pass explicit timeouts (`timeout=20` for search, `timeout=30` for extract) and wrap both calls in the same jittered retry helper as #01 (2 retries on 429, 5xx and connection errors, honoring `Retry-After`). Split extraction into batches of 5 run in parallel, so one slow page delays fewer others.

### #03 [P1] A provider that's down is hit again by every query of every check
- Location: search fan-out `src/priorart/pipeline.py:129–137`; comparisons `:301–308`; verification `:322–330`; no shared provider state anywhere.
- Risk: When Tavily or Token Factory is down, out of quota or rejecting the key, every running check still fires its 14 searches and about 19 model calls, each waiting out timeouts and retries. Several users at once multiply the load on a struggling provider and each waits minutes for a failure that was known after the first call.
- Fix: A per-provider circuit breaker, one instance per process, shared by all checks:
  - **closed:** normal;
  - **open:** after 5 failures within 30 s, or immediately on 401, 402 or a quota error; calls fail at once with `ProviderUnavailable("search")`;
  - **half-open:** after 60 s, one trial call.

  An open breaker shows up as `degraded` in `/readyz` (19.3) and as a clear banner ("Search is unavailable right now. Saved reports still open."). A check that starts while search is open stops immediately with no credits spent. Unit tests for each state change.

### #04 [P1] A check has no overall deadline
- Location: `src/priorart/pipeline.py:264–401` (`run`, with no time budget); `as_completed(futs)` without a timeout `:131`, `:303`, `:324`; each `with ThreadPoolExecutor` block waits for every future on exit.
- Risk: One hung call (a socket that never returns data, or #01's worst case) holds the whole check, its thread and the user's open stream indefinitely. The page shows "Comparing…" forever.
- Fix: A deadline per check (`PRIORART_CHECK_DEADLINE_S`, default 240 s) with per-stage budgets. Pass `timeout=remaining` to `as_completed`; on `TimeoutError`, cancel the futures that haven't started, stop waiting for running ones (`pool.shutdown(wait=False, cancel_futures=True)`), and continue with the results gathered so far, adding a warning ("3 projects were skipped because the model service was too slow"). Projects that weren't compared are left out of the grid rather than marked open. Test with a fake client that never returns.

## Unexpected shapes and missing values

### #05 [P1] A model reply that is valid JSON but not an object crashes the check
- Location: `src/priorart/llm.py:20–37` (`parse_json` returns arrays as well as objects); unguarded `.get` on the result `src/priorart/pipeline.py:102`, `:110`, `:121` (decompose), `:163–166` (triage).
- Risk: A model that answers `[{"id": "c1", ...}]` instead of `{"picked": [...]}` raises `AttributeError: 'list' object has no attribute 'get'`. In decompose or triage that fails the whole check, after 14 paid searches in triage's case.
- Fix: `llm.json(..., expect=dict)`: if the parsed value isn't a dict, wrap a bare list under the task's expected key (`picked`, `features`, `results`, `pivots`) when that's unambiguous, otherwise make the one repair call, then raise `LLMError`. Validate each task's output with a small Pydantic model that has defaults, so a missing field becomes an empty list rather than a crash. Tests with lists, `null`, strings and missing keys for every task.

### #06 [P1] A triage failure throws away the paid searches
- Location: `src/priorart/pipeline.py:163` (`self.llm.json("triage", ...)`, not wrapped); the fallback to top results `:172–173` runs only when the model answers with nothing usable, not when it fails.
- Risk: If the triage call times out or errors, the check fails with an error after all searches have been paid for, and the user gets nothing.
- Fix: Wrap the call; on failure emit a warning ("Couldn't rank the results, so the top search results were used") and use the existing fallback (`candidates[: max_candidates]` by search score). Test with a fake model that raises on the triage task only.

### #07 [P2] A failure before the first search leaves no report at all
- Location: decompose raises `RuntimeError` when it gets no usable features `src/priorart/pipeline.py:107–108`; any model error there propagates out of `run` (`:268–272`) to the server's catch-all `src/priorart/server.py:87–88`; nothing is saved.
- Risk: The user gets a one-line error, no record in Past runs, and no hint whether retrying will help. The same idea will hit the same failure, since nothing distinguishes "the model service is down" from "the idea is too vague".
- Fix: Classify the failure (`provider_unavailable`, `idea_too_vague`, `internal`) and save a `failed` report with that cause and message through the same `_finish` path the other early stops use, so it shows in Past runs with a **Try again** or **Describe it differently** action.

### #08 [P1] A configuration typo takes down every request
- Location: bare `int(os.getenv(...))` in `src/priorart/config.py:42–45`; `Settings()` is built on every request `src/priorart/server.py:27`, `:34`, `:49`, `:76`; `PRIORART_WORKERS=0` reaches `ThreadPoolExecutor(max_workers=0)` (`src/priorart/pipeline.py:129`), which raises `ValueError`; an unknown `PRIORART_SIMULATE` raises inside every `build()` (`src/priorart/__init__.py:39–41`).
- Risk: One mistyped value (`PRIORART_MAX_CANDIDATES=8 ` with a stray character, or `=eight`) makes the status endpoint, Past runs and every check return 500, so the whole site is down. The error only appears when the first request arrives.
- Fix: Build `Settings` once at startup with validation and bounds (`workers` 1–16, `max_candidates` 1–20, `page_chars` 1,000–50,000, `simulate` from the allowed list); on any error, exit with one readable line naming the variable and the allowed range. Requests use that single validated instance. Tests for each bad value.

## Silent failures and error recording

### #09 [P1] Failures are swallowed or reported only to the browser
- Location: the check worker's catch-all `src/priorart/server.py:85–90` (sends `str(e)` to one browser and records nothing); unreadable run files skipped without a trace `:43–44`; no logger anywhere in the server or pipeline.
- Risk: On launch day, a failing check leaves no stack trace, no count and no way to find the cause; a corrupt run file disappears from Past runs without notice. Raw exception text shown to the user can include paths and provider responses (S.03 #14).
- Fix: Log every caught exception with its traceback, the check id and a short reference id (19.1 / 19.2 design); send the user a fixed message for the failure class plus the reference. For skipped run files, log the file name and reason once, and expose a count in `/readyz` details.

### #10 [P1] A corrupt or older report file breaks its endpoints with a 500
- Location: `src/priorart/server.py:56–58` and `:61–65` (`json.loads(...)` with no handling); `to_markdown` indexes keys directly `src/priorart/report.py:20–63` (`r['idea']`, `r['checks']`, `r['models']`, …).
- Risk: A truncated file (#01 in S.04) or a run saved by an earlier version (missing `checks`, `models` or `gaps`) gives a bare 500 for both viewing and downloading, and the page then renders that error body as a report (S.02 #10).
- Fix: One `load_report(path)` that parses, checks a `schema_version`, fills defaults for every field the page and exporter read, and raises `ReportUnreadable` on failure; routes turn that into `410 {"error": "This report can't be opened"}`. Add `schema_version: 1` to new results. Tests with a truncated file and a minimal old-format file.

### #11 [P1] A completed check is lost if saving fails
- Location: `src/priorart/pipeline.py:400–401` (`saved_to = self.save(result)` runs before `emit("done", result)`). Same issue as S.02 #11.
- Risk: A full disk or a permissions error turns a finished, paid two-minute check into "Run failed", although the complete result was already in memory.
- Fix: Emit `done` first, then save in a `try`; on failure emit a warning ("This report couldn't be saved. Download it now.") and keep the result in a bounded in-memory map (last 50, 1 hour) so the Markdown download still works; log the failure.

## The page

### #12 [P1] One unexpected field leaves the report half-drawn
- Location: `render()` fills about ten sections with no error handling `src/priorart/static/index.html:273–340` (for example `r.coverage[f.id]` at `:304`, `r.checks` at `:294`, `r.gaps` at `:328`); `openCell` assumes the project exists `:343` (`p.cells[fid]` when `p` is undefined).
- Risk: A report missing one field (an older run, a partial save) throws partway through: sections before the error show the new report, sections after it still show the previous report or nothing, and the evidence drawer stops opening.
- Fix: `normalize(r)` first (default every array and object the page reads). Render each section through `section(el, fn)`, which catches errors, shows "This part couldn't be shown. Try again." in that section only, and reports the error (19.2). In `openCell`, return early with a message if the project or feature isn't found.

### #13 [P1] Malformed events and failed requests kill handlers silently
- Location: `JSON.parse(e.data)` in every stream listener `src/priorart/static/index.html:248–261`; promise chains with no `.catch` and no `r.ok` check `:201–206`, `:208–212`, `:214`; no `window.onerror` or `unhandledrejection` handler.
- Risk: One bad event payload throws inside its listener: the progress stops updating while the check keeps running, and nothing tells the user. Failed requests become unhandled rejections and blank controls.
- Fix: A `safeParse(e.data)` that returns `null` and reports on failure, used by every listener; one `api()` fetch helper that checks `r.ok` and rejects with the server's message, used everywhere; global `error` and `unhandledrejection` handlers that show an app-level banner with **Reload** and report the error.

### #14 [P1] A dropped connection looks like a finished check
- Location: `src/priorart/static/index.html:256–259` (the browser's connection error and the server's `error` event share one handler; without data it just calls `finish()`). Same issue as S.02 #09.
- Risk: The progress stops and the button re-enables with no message while the check keeps running on the server, so users start a second, paid check.
- Fix: Rename the server's event to `check_error` (`src/priorart/server.py:88`); treat the browser's `error` event as "Connection lost" with a **Reconnect** button that resumes from stored events (S.02 #07); never re-enable **Check idea** silently.

## Streams and background work

### #15 [P1] The event stream has no heartbeat, so proxies can cut it during slow stages
- Location: `src/priorart/server.py:94–99` (events are sent only when the pipeline emits); the pivots stage is one model call with no events for its duration `src/priorart/pipeline.py:366–370`, and a slow comparison batch can also go quiet for a long time.
- Risk: Reverse proxies and load balancers close connections that stay silent past their idle timeout (often around 60–100 seconds). A check that's still working looks dead to the browser and the stream drops near the end.
- Fix: In `stream()`, wait for the next event with a 15-second timeout and send an SSE comment (`: ping\n\n`) when it expires; send `retry: 5000` once at the start. With #16's async queue this is `asyncio.wait_for(queue.get(), 15)`.

### #16 [P1] Each open stream pins a thread, and threads leak when browsers leave
- Location: `await asyncio.to_thread(q.get)` `src/priorart/server.py:96` (a blocking wait in the shared thread pool for each stream); check threads are started detached and untracked `:91` (`daemon=True`).
- Risk: About 40 open streams use up the default thread pool, and then every other request that uses it, health checks included, stalls. When a browser disconnects, its waiting thread stays blocked until the check produces another event, and forever if the check hangs (#04).
- Fix: Replace `queue.Queue` with an `asyncio.Queue` fed from the worker through `loop.call_soon_threadsafe(q.put_nowait, item)`, so waiting costs no thread. Track check threads in a registry with their start time, and cap them (#18). When the stream's generator is cancelled, mark the check "no listener", so the pipeline keeps going and saves (the user can reopen it), with no thread waiting on the browser.

### #17 [P2] Restarts kill running checks without a trace
- Location: `daemon=True` check threads `src/priorart/server.py:91`; `serve` runs plain `uvicorn.run` with no shutdown handling `src/priorart/__main__.py:128–132`.
- Risk: Any restart (a deploy, a crash, Ctrl-C) ends every running check mid-way. Their credits are spent, nothing is saved, and their streams end with no message.
- Fix: Non-daemon check threads in the registry; on SIGTERM, stop accepting checks, wait up to a drain limit for running ones to finish and save, then send remaining streams a `check_error` ("The server restarted during this check. Run it again; finished searches are cached."). Detailed in the 19.3 plan.

## Duplicates and optional services

### #18 [P1] Any GET request to the check URL starts a new paid check
- Location: `src/priorart/server.py:72–92` (`GET /api/run?idea=…` starts work, no deduplication); `src/priorart/static/index.html:247`.
- Risk: Link previews (chat apps unfurling a pasted URL), browser prefetching, a second tab or a retry after a network blip each start another full check for the same idea, spending credits each time.
- Fix: Start checks with `POST /api/checks` carrying an `Idempotency-Key` header (a UUID per button press): the same key returns the same check id (S.04 #06). Additionally, if the same owner starts the same idea (hash of the normalized text) while an identical check is running, attach to the running one instead of starting another. Cap concurrent checks per process (S.03 #05) and return 429 when full.

### #19 [P1] The optional cache can fail a check
- Location: `src/priorart/cache.py:21–25` (opening the file is required, so a bad path or permission fails `build()` for every check, `src/priorart/__init__.py:27`); a lock or disk error inside `get_or_compute` `cache.py:38–44` propagates into the search or model call that used it.
- Risk: The cache exists to save credits, but a full disk, a locked file or a permissions change turns it into a reason every check fails.
- Fix: Treat the cache as best-effort: if it can't open, log once and run with caching off; wrap the read and write inside `get_or_compute` so any `sqlite3.Error` counts as a miss (read) or is logged and ignored (write). Test by pointing the cache at an unwritable path and at a file held under an exclusive lock: checks still finish.

### #20 [P2] No tests inject the failures this list describes
- Location: the fake clients simulate only "everything down" (`src/priorart/__init__.py:29–51`, `search_down` / `extract_down`; `src/priorart/fakes.py`); `tests/test_pipeline.py` covers full outages but not slow, flaky or malformed responses.
- Risk: The fixes above can regress silently. Today nothing proves a check survives a 429 followed by success, a hung call, a list instead of an object, or an event stream that goes quiet.
- Fix: A `FaultyClient` wrapper for tests with programmable faults per task and call number (latency, raise a given error, return a list, return `null`, never return). Tests assert a check finishes within its deadline with the right warnings for: one 429 then success; a hung compare call; triage failure; non-object JSON; cache locked; the stream heartbeat arriving during a 40-second quiet stage.

## Suggested batches

| Batch | Items | Notes |
|---|---|---|
| **A: before judging** | #05, #06, #08, #13, #14, #19 | Stops whole-check crashes and silent page failures; about 2 hours with tests |
| B: provider resilience | #01, #02, #03, #04, #20 | One retry and deadline policy, a breaker, and the fault tests that prove it |
| C: streams and threads | #15, #16, #17, #18 | Heartbeat, async queue, drain, deduplication |
| D: recording and recovery | #07, #09, #10, #11, #12 | Logs with references, tolerant report loading, no lost results |

# Prior Art — Performance, Latency & Cold-Start (S.07)

Profiled 2026-11-20 on this machine, offline demo mode (fake model and search clients), plus static analysis of the real-provider path.

## Measurements

| Measure | Result | How |
|---|---|---|
| Full check, demo clients | 0.86 s | `build(Settings()).run(...)` |
| Saved run file (demo report) | 10,265 bytes | file size |
| `GET /api/runs` with 2,001 saved runs | **13.1 ms** median | 20 requests, httpx |
| `GET /api/runs` with 20,001 saved runs | **145.0 ms** median | 10 requests |
| `GET /` | 2.2 ms; **24,494 bytes, no compression** (gzip would be 7,698, −69%); ETag but no `Cache-Control` | response headers |
| `GET /api/status` | 1.5 ms | |
| Import `priorart.server` | 358 ms (FastAPI 244 ms of it) | `python -X importtime` |
| Import `openai` + `tavily`, paid inside the first check | **640 ms + 166 ms** | `python -X importtime` |
| API cache write | 1.31 ms each (rollback journal, commit per write) | 200 writes, 9 KB values |
| `render()` of a full report in Chrome | **3.97 ms** average | 20 renders, Playwright |

**What is not a bottleneck, so it isn't on the list:**
- Rendering takes 4 ms.
- Every list is small and bounded: at most 8 projects × 7 features, 8 sources, 30 past runs and about 50 log lines, so virtualization would add complexity for nothing.
- There are no images, no bundle and no barrel imports (one 24 KB page).
- Startup is 0.36 s.

**The dominant latency is the check itself:** about two minutes with real providers (`README.md`), made of model and search calls. Savings on that path below are **estimates** from the call structure. Real per-call latencies aren't measured yet, because that needs live keys; 19.4's per-call metrics would measure them. The structure of the critical path today:

```
decompose (1 call) → 14 searches (6 at a time) → triage (1 call) → extract (1 batch, waits for all pages)
  → compare 8 projects (6 at a time) → [wait for all] → verify 8 projects (6 at a time) → pivots (1 call)
```

## The check's critical path

### #01 [P1] The 14 searches run in three sequential waves
- Location: `src/priorart/config.py:45` (`workers` default 6) used for the search pool `src/priorart/pipeline.py:129–130`; up to 14 queries (`:122`).
- Risk: With 6 threads, 14 Tavily advanced searches finish in ⌈14/6⌉ = 3 waves, so the search stage takes about three search latencies instead of one. These calls are network-bound, so the thread count is the only limit.
- Fix: Size I/O pools by the work, not a global setting: `ThreadPoolExecutor(max_workers=min(len(queries), settings.search_concurrency))`, with `search_concurrency` defaulting to 14 and capped against provider rate limits by 17.1's limiter. **Estimated saving: 2 search latencies, about 4–12 s per check.**

### #02 [P1] Comparisons and verifications each run in two waves
- Location: `max_candidates` 8 (`src/priorart/config.py:42`) against `workers` 6 (`:45`); compare pool `src/priorart/pipeline.py:301`, verify pool `:322`.
- Risk: Projects 7 and 8 start only after one of the first six finishes, so each of these two stages costs about two model-call latencies instead of one.
- Fix: `max_workers=len(picked)` for comparisons and `len(projects)` for verification (both ≤ `max_candidates`), with a per-provider concurrency cap from configuration. **Estimated saving: one compare call plus one verify call, about 10–30 s.**

### #03 [P1] Verification waits for every comparison to finish
- Location: compare everything `src/priorart/pipeline.py:301–314`, then verify everything `:322–334`.
- Risk: The slowest comparison holds back the verification of all the others, so the critical path is (slowest compare) + (slowest verify), when it could be the slowest single (compare + verify).
- Fix: One task per project, `compare_one` then `verify_one`, submitted together; `compared` and `verified` events are emitted as each project progresses, and totals are summed at the end. **Estimated saving: 5–15 s when one comparison is a straggler.** It also lets the grid fill row by row (#16).

### #04 [P1] Unsupported JSON mode is rediscovered by every check and every parallel call
- Location: `self._json_mode_ok` lives on the `LLM` instance `src/priorart/llm.py:47`, checked and set at `:59–68`; a new `LLM` is built for every check `src/priorart/__init__.py:28`.
- Risk: For a model that rejects `response_format`, each check's first wave of calls (up to 6–8 in parallel, before any of them records the failure) makes one rejected request each, then retries without JSON mode. That costs one wasted round trip per call in that wave, on every check.
- Fix: Keep the support map at process level (`_JSON_MODE: dict[str, bool]` shared by the one `LLM` from #05), seed it from configuration or probe each model once at startup, and guard the first discovery with a lock so only one call pays for it. **Estimated saving: one round trip (about 0.3–1 s) for each first-wave call, per check.**

### #05 [P1] New API clients for every check
- Location: `src/priorart/__init__.py:27–28` (new `Cache`, `LLM`, `Search` in every `build()`); `OpenAI(...)` `src/priorart/llm.py:46`; `TavilyClient(...)` `src/priorart/search.py:26`.
- Risk: Every check starts with empty connection pools, so it pays fresh TCP and TLS handshakes to both providers instead of reusing warm keep-alive connections; each check also leaves its pools and a cache connection behind (S.01 #11).
- Fix: One `Cache`, one `LLM` and one `Search` per process, created in the FastAPI lifespan (the clients are thread-safe; the cache already locks), passed into each `PriorArt`; `httpx.Limits(max_connections=32, max_keepalive_connections=16)` for the model client and a shared `requests.Session` with a sized adapter for Tavily. **Estimated saving: about 100–300 ms per new connection, several per check, and no handshake storms under load.**

### #06 [P1] No comparison starts until every page has been read
- Location: one extraction batch for all picked pages `src/priorart/pipeline.py:296–297` → `fetch_pages` `:176–190` → `src/priorart/search.py:52–61` (one call for up to 20 URLs, 30 s default timeout).
- Risk: The slowest page in the batch (often a heavy product site) delays every comparison, even for pages that were ready in a second.
- Fix: Extract in small parallel batches (2–3 URLs) and chain each project's compare to its own page's arrival (futures: `extract_future.add_done_callback` → submit `compare_then_verify`). A project whose page fails falls back to its snippet immediately, as today. **Estimated saving: 3–10 s, more when one site is slow.**

### #07 [P2] The slowest search holds up everything after it
- Location: `as_completed(futs)` with no time limit `src/priorart/pipeline.py:131`; triage starts only after every search returns `:291–293`; Tavily's default search timeout is 60 s.
- Risk: One hanging query delays the whole check by up to a minute, even when the other 13 results are enough to rank.
- Fix: An explicit 20 s per-search timeout (S.06 #02) plus a soft stage budget: when all searches are done, or 15 s have passed with at least 80% done, continue to triage, cancel the rest, and note "2 searches were skipped for time". **Estimated saving: up to 40 s in the bad case; none in the normal case.**

### #08 [P2] Each comparison sends up to 9,000 characters of page text
- Location: `page_chars` 9,000 `src/priorart/config.py:44`; the page is cut to that length `src/priorart/pipeline.py:186` and sent whole in the compare payload `:194`.
- Risk: Model latency and cost grow with input size; most of a product page is navigation, pricing and boilerplate unrelated to the features being checked.
- Fix: Keep the full page for the code quote check, but send the model only the relevant passages: split into paragraphs, score each by overlap with the feature names and descriptions, keep the best ones up to 4,000 characters in page order, and always include the first 800 characters (the page's own description). Measure verified-claim recall on a fixed set of ideas before switching the default. **Estimated saving: 30–50% of compare input tokens; the latency effect depends on the provider and is to be measured.**

### #09 [P2] A malformed reply doubles the call
- Location: the repair call `src/priorart/llm.py:105–114` (a second full call that sends back up to 6,000 characters).
- Risk: Every reply that isn't valid JSON costs a second model round trip, on the critical path.
- Fix: Prefer the provider's JSON or structured-output mode wherever it's supported (#04), cap the repair call's `max_tokens` at the original output's length, and count repairs per model and task (19.4). If one model needs repairs often, change its prompt or swap the model for that task. **Saving: one full call per malformed reply.**

### #10 [P2] Identical concurrent checks pay twice and take twice as long
- Location: `src/priorart/cache.py:36–45` (the lock doesn't cover the computation); no deduplication of running checks `src/priorart/server.py:72–92`.
- Risk: During a demo, judging or a shared link, several people often check the same sample idea at once. Each one runs every call in full, competing for the same provider rate limit, so all of them get slower.
- Fix: Single-flight per cache key (waiters block on the first computation, S.04 #10), and attach a second request for the same normalized idea to the check already running (S.06 #18). **Saving: the entire second check's time and cost.**

## Server hot paths

### #11 [P1] The Past runs list gets slower with every saved run
- Location: `src/priorart/server.py:35–44` (sorts every file name in the folder, then reads and parses 30 full reports); called on every page load and after every finished check (`src/priorart/static/index.html:213`, `:261`).
- Risk: Measured: 13.1 ms at 2,001 runs and 145.0 ms at 20,001, growing linearly. That's on every Home visit, and it runs in the request thread pool (#19).
- Fix: Serve the list from an index: the database's `recent_reports` query (indexed, LIMIT 20) once S.04 #03 lands, or until then an append-only `runs/index.jsonl` of `{file, summary, status, novelty, created}` written together with each report, reading only its last 30 lines (seek from the end). **Saving: about 145 ms → about 1 ms at 20,000 runs, constant from then on.**

### #12 [P2] Report views and downloads re-read and re-parse files every time
- Location: `src/priorart/server.py:56–58` (`json.loads(... read_text())`), `:61–65` (read, parse and `to_markdown` on every download).
- Risk: Every reopen, refresh and download repeats the disk read, the parse and the Markdown conversion, and the browser always downloads the full body again.
- Fix: An LRU of parsed reports keyed by `(path, mtime_ns)` (128 entries); a strong `ETag` from that key plus `Cache-Control: private, no-cache` so revisits get `304 Not Modified`; cache the Markdown alongside. **Saving: the parse and Markdown work on repeat views, and the whole body on a 304.**

### #13 [P2] Nothing is compressed
- Location: no compression middleware `src/priorart/server.py:17`; measured `GET /` returns 24,494 bytes with no `Content-Encoding`.
- Risk: The page is three times larger on the wire than it needs to be (gzip: 7,698 bytes, −69%), and JSON responses (reports, Past runs) are sent uncompressed too. It matters most for phones on slow networks.
- Fix: `app.add_middleware(GZipMiddleware, minimum_size=1024)`, excluding `text/event-stream` so the progress stream isn't buffered (send the stream with `Content-Encoding: identity`). Prefer Brotli at the proxy if the host provides it. **Saving: about 17 KB on first load; roughly 60–80% of JSON bytes.**

### #14 [P2] The page has no caching policy
- Location: `GET /` returns `FileResponse` with an ETag but no `Cache-Control` (`src/priorart/server.py:20–22`); styles and script are inline in the HTML (`src/priorart/static/index.html:7–110`, `:194–368`), so they can't be cached separately.
- Risk: Without explicit headers, browsers apply heuristic caching: sometimes stale after a deploy, sometimes a full re-download. Every HTML change re-sends all the CSS and JavaScript.
- Fix: `Cache-Control: no-cache` on the HTML (always revalidate, served as a 304 of about 300 bytes when unchanged); move the CSS and JS into content-hashed files under `/static/` with `Cache-Control: public, max-age=31536000, immutable` (the 18.2 plan). **Saving: repeat visits cost a 304 instead of 24 KB.**

### #15 [P2] The page refetches the whole Past runs list after every check
- Location: `loadPast()` called on `done` `src/priorart/static/index.html:261` (request to `/api/runs`, #11).
- Risk: The finished report is already in the browser, but the page asks the server to rebuild the whole list (145 ms of server time at 20,000 runs) just to add one entry.
- Fix: Prepend the finished report to the list from the `done` payload (`{file, summary, novelty, status, created}`) and refresh the full list only on page load. **Saving: one list request per check.**

## The waiting experience

### #16 [P1] Results appear only when the whole check is done
- Location: results hidden at start `src/priorart/static/index.html:241`; everything renders on `done` `:261`, `:273`; stream events only add log lines `:248–255`.
- Risk: Time to the first useful content equals the full check time (about two minutes), although the features are known after the first call and each project's row is known after its comparison. That's the largest *perceived* latency in the product.
- Fix: Render progressively from the events the server already sends: feature headers on `plan`, project rows (skeleton cells) on `triage`, claimed cells on `compared`, final cells on `verified`; the score, verdict and pivots on `done`. With #03 each row completes independently. **Estimated saving: first project visible at about 40–60% of the check's duration instead of 100%; no change to total time.**

### #17 [P2] Opening a past report waits on a blank screen
- Location: `src/priorart/static/index.html:214` (fetch the full report, then render; nothing shown meanwhile).
- Risk: On a slow connection, choosing a past report shows nothing until the whole JSON arrives, so it feels unresponsive.
- Fix: Render the summary, status and score from the list entry immediately with skeleton sections, then fill in when the fetch resolves. Prefetch the three most recent reports at idle time (`requestIdleCallback`) so the common case opens instantly. **Saving: perceived wait close to zero for recent reports.**

### #18 [P2] The first check after a start pays for library imports
- Location: `from openai import OpenAI` inside `LLM.__init__` `src/priorart/llm.py:42`; `from tavily import TavilyClient` inside `Search.__init__` `src/priorart/search.py:22`.
- Risk: Measured 640 ms (openai) + 166 ms (tavily) of imports inside the first check after every deploy or restart, plus that check's DNS and TLS setup to both providers.
- Fix: Create the process-wide clients (#05) during startup (the 19.3 warm-up step), before readiness turns green, and open one connection to each provider with a cheap request (Token Factory's model list; no Tavily credits spent). **Saving: about 0.8 s plus handshakes on the first check after each start.**

### #19 [P1] Open progress streams starve the server's request threads
- Location: every endpoint is a plain `def` (`src/priorart/server.py:20–65`), so FastAPI runs them in the shared worker-thread pool (40 threads by default); each open stream also occupies one of those threads in `await asyncio.to_thread(q.get)` `:96`.
- Risk: With about 40 streams open (40 people waiting on checks), every other request (Home, status, Past runs, report downloads) queues behind them, so the whole site looks frozen while checks are running. This is a cliff, not a gradual slowdown.
- Fix: Feed the stream from an `asyncio.Queue` via `loop.call_soon_threadsafe` (no thread while waiting, S.06 #16); make the fast file endpoints `async def` using a small dedicated executor for disk reads. Add a load test that opens 60 streams and asserts `/api/status` stays under 50 ms. **Saving: removes the cliff; status and list latency stay flat as streams grow.**

### #20 [P2] Every task uses the same two large models
- Location: writer model for decompose, triage, compare and pivots, verifier model for verify (`src/priorart/config.py:35–40`; `src/priorart/pipeline.py:100`, `:163`, `:195`, `:227`, `:259`).
- Risk: Triage (ranking about 60 short results) and pivots (a few sentences) don't need a 70B-class model, and the verifier is a large model on every project. Large models are slower per token, and all of these calls are on the critical path.
- Fix: Measure per-task latency and quality (19.4 metrics plus a fixed set of ten ideas with known overlaps), then allow per-task model overrides (`PRIORART_MODEL_TRIAGE`, `PRIORART_MODEL_PIVOTS`), keeping a different model family for the verifier. Adopt a smaller model only where verified-claim results don't drop. **Saving: to be measured; it could be the largest single gain after #01–#03.**

## Suggested batches

| Batch | Items | Estimated effect |
|---|---|---|
| **A: today, small changes** | #01, #02, #04, #05, #13 | About 15–45 s off a real check (estimated), less wasted traffic; mostly configuration and wiring |
| B: restructure the critical path | #03, #06, #07, #16 | Another 10–30 s off, and results visible from the halfway point |
| C: server hot paths | #11, #12, #14, #15, #19 | Flat latency as runs and users grow |
| D: measure, then tune | #08, #09, #10, #17, #18, #20 | Needs live-key measurements (19.4) |

# Prior Art — Anti-Wallet-Drainer, Quotas, Billing & Abuse Guardrails (S.08)

Scanned 2026-11-20: every route in `src/priorart/server.py`, the CLI (`src/priorart/__main__.py`), the pipeline (`src/priorart/pipeline.py`), both paid clients (`src/priorart/llm.py`, `src/priorart/search.py`), the cache (`src/priorart/cache.py`), configuration and `.env.example`.

## What costs money

| Paid resource | Who pays | Triggered by |
|---|---|---|
| Nebius Token Factory tokens (two models) | the operator's key (`.env.example:2`) | `GET /api/run` (`server.py:72`), the CLI (`__main__.py:135`) |
| Tavily credits (advanced search and extract) | the operator's key (`.env.example:3`) | the same two triggers |
| Disk (run files, API cache) | the host | every check |

**There is no billing code at all:** no payments, subscriptions, plans, entitlements, webhooks, email or SMS. A search for `stripe|webhook|smtp|sendgrid|twilio|email|billing|subscription|entitle|quota|rate limit` across `src/` and `scripts/` finds only a comment about provider rate limits (`llm.py:92`). Users pay nothing; every check is paid by the operator. So the Day-1 economic risk is **one anonymous visitor spending the operator's credits.** Payment-webhook guidance appears once (#19), for when a paid tier is added.

## Worst-case cost of one check, from the code (default settings)

- **Model calls:** 1 decompose (`pipeline.py:100`) + 1 triage (`:163`) + up to 8 compare (`:195`, `max_candidates` 8 at `config.py:42`) + up to 8 verify (`:227`) + 1 pivots (`:259`) = **19 calls**. Each can add one repair call (`llm.py:105–114`), giving **38**, and each attempt can be retried up to 9 times (S.06 #01).
- **Output-token ceiling:** 2,500 + 1,500 + 8 × 1,800 + 8 × 1,500 + 1,200 = **31,600 tokens**, or **63,200** with repairs.
- **Input:** up to 9,000 characters of page text per comparison (`config.py:44`) and up to 60 search results in the triage prompt (`pipeline.py:146`).
- **Tavily:** up to 14 advanced searches (`pipeline.py:122`) at 2 credits each (`README.md:41`) plus extraction of 8 pages (2 credits per 5 pages) = **about 32 credits per check.**
- **No limit on how many checks:** at one request per second from one script, that is **3,600 checks and about 115,000 Tavily credits an hour.**

No dollar figures are given because provider prices weren't verified here. 19.4's usage capture (#06) would make them exact.

## Rate, concurrency and spend limits

### #01 [P0] Anyone can start unlimited paid checks
- Location: `src/priorart/server.py:72–92` (no limit of any kind before `build(settings).run(idea)` in a new thread).
- Risk: One script in a loop, or one popular post linking the site, spends the operator's Token Factory and Tavily balances at about 32 credits and up to 19 model calls per check, with nothing to stop it until a provider refuses the key. Real users then get failures for the rest of the day.
- Fix: Two token buckets checked before any work starts, in one `guard_check_start(request)` dependency:
  - **per client:** 3 checks per 10 minutes, 15 per day, keyed by owner token (S.03 #01) and also by IP address taken from the trusted proxy header only;
  - **global:** 60 checks per hour.

  A rejected request gets `429` with `Retry-After` and "You've reached today's limit of 15 checks. It resets at 00:00 UTC." Buckets live in SQLite (`rate_buckets(key TEXT PRIMARY KEY, tokens REAL, updated_at REAL)`), updated in a `BEGIN IMMEDIATE` transaction so blue-green processes share them. Tests: the 4th check within 10 minutes is refused; the bucket refills over time.

### #02 [P0] No concurrency cap and no hard spend ceiling
- Location: `src/priorart/server.py:85–91` (unbounded threads, one per request); no running total of spend anywhere.
- Risk: Even with rate limits, a burst of distinct clients can run hundreds of checks at once, and nothing stops spending when the day's budget is gone.
- Fix: A process-wide semaphore of 4 running checks (configurable; the rest get "Prior Art is busy, try again in a minute"). A daily budget in settings (`PRIORART_DAILY_BUDGET_CREDITS`, `PRIORART_DAILY_BUDGET_OUTPUT_TOKENS`), checked against the usage ledger (#06) before each check and before each call. At 80%: alert (19.5). At 100%: refuse new checks with an honest message and finish running checks with what they have. Resets at 00:00 UTC.

### #03 [P0] A GET request starts paid work, so link previews and prefetching spend credits
- Location: `@app.get("/api/run")` with the idea in the query string `src/priorart/server.py:72–73`; caller `src/priorart/static/index.html:247`.
- Risk: Chat apps that unfurl a pasted URL, browser prefetching, crawlers and any website embedding the URL in an `<img>` tag each start a full paid check without a user pressing anything.
- Fix: `POST /api/checks` only, with a JSON body, a same-origin check and an `Idempotency-Key` header; streaming moves to `GET /api/checks/{id}/events`, which never starts work (S.03 #04). The old GET route returns `405`.

### #04 [P1] A reconnecting browser could start the same check again
- Location: `EventSource` on the URL that starts a check `src/priorart/static/index.html:247`; reconnection is prevented only because every handler calls `finish()` (`:256–261`); the server starts a new check on any request to that URL `src/priorart/server.py:72–92`.
- Risk: Browsers automatically reconnect an `EventSource` after a dropped connection. If a handler throws before `finish()` runs (for example a parse error in the error handler at `:257`), the browser re-requests the same URL a few seconds later, and **each reconnect starts another full paid check,** repeating for as long as the page stays open.
- Fix: Never start work on a stream URL (#03). Until then, the server refuses to start a check when the request carries a `Last-Event-ID` header (a reconnect) and returns `204`; the page wraps every handler in `try { … } finally { … }` so the stream is always closed.

## Duplicate work

### #05 [P1] Identical and near-identical checks are paid for again
- Location: cache key on the exact request text `src/priorart/llm.py:117` (`{"m", "t", "s", "u": user}`, where `user` is the raw JSON payload); the idea is only `strip()`ped (`src/priorart/pipeline.py:266`); no in-flight deduplication (`src/priorart/cache.py:36–45`).
- Risk: "Build a research agent." and "build a research agent" (or the same idea with an extra line break) miss the cache and pay in full. Two people checking the same sample idea at the same moment both pay in full.
- Fix: Normalize the idea before it reaches any prompt or cache key: Unicode NFC, collapse runs of whitespace, trim trailing punctuation. Single-flight on each cache key (waiters block on the first computation). Attach a request for an idea already being checked by the same owner (or, for public sample ideas, by anyone) to that running check instead of starting another. Tests: three spacing variants of one idea produce one set of paid calls.

## Usage tracking and attribution

### #06 [P1] Usage reported by the providers is thrown away
- Location: `src/priorart/llm.py:62` and `:69` (only `choices[0].message.content` is kept; `response.usage` is discarded); `src/priorart/search.py:38–46`, `:56–60` (Tavily's `include_usage` is never requested).
- Risk: The operator can't tell what a check, a day or a single abusive client costs, and the spend cap (#02) has nothing real to count.
- Fix: A `usage_events` table: `ts`, `check_id`, `owner_ref` (hashed), `ip_ref` (hashed, rotated daily), `provider`, `operation`, `model`, `prompt_tokens`, `completion_tokens`, `credits`, `cached` (true for cache hits, which cost 0), `attempt`. Write a row for every billed call, including failed attempts that returned usage. Store totals on each report. Feed #02's budget check and #20's report from it.

### #07 [P1] Retries and repairs can bill the same call many times
- Location: the client's own 2 retries plus our 3-attempt loop `src/priorart/llm.py:46`, `:79–96`; the repair call `:105–114`.
- Risk: A request that times out on the client after the model finished generating may still be billed, and the same call can be sent up to nine times during a provider slowdown. A malformed reply always doubles its call's cost. None of it is counted or capped.
- Fix: One retry policy (`max_retries=0` on the client; up to 3 attempts, retrying only connection errors, timeouts, 429 and 5xx, with jitter; S.06 #01). Count every attempt against the check's budget (#09) and the daily budget (#02). Cap the repair call's `max_tokens` at the original reply's length, and record the repair rate per model in the usage table.

## Per-check and per-call caps

### #08 [P1] Cost settings have no upper bounds
- Location: `src/priorart/config.py:42–45` (`max_candidates`, `results_per_query`, `page_chars`, `workers` read with bare `int(...)`, no limits).
- Risk: A typo such as `PRIORART_MAX_CANDIDATES=80` makes every check about ten times as expensive (80 comparisons and 80 verifications), silently, for every user.
- Fix: Validated bounds at startup (`max_candidates` 1–12, `results_per_query` 1–10, `page_chars` 1,000–12,000, `workers` 1–16); refuse to start outside them. Log the resulting worst-case cost per check (calls, output tokens, credits) once at startup, so a costly configuration is visible.

### #09 [P1] A check has no budget object
- Location: `src/priorart/pipeline.py:264–401` (`run` calls the model and search clients with no running total); the worst case above is only implied by scattered constants.
- Risk: Any change (more queries, more candidates, an extra stage, a retry storm) silently raises the cost of every check with nothing to stop it.
- Fix: A `CheckBudget(max_model_calls=24, max_output_tokens=40_000, max_input_chars=150_000, max_tavily_credits=40)` passed to both clients. Each call reserves its `max_tokens` and estimated input before it's sent and records the actual usage after. When the budget would be exceeded, the call raises `BudgetExceeded` and the pipeline finishes with what it has ("Stopped early to stay within the cost limit"). Tests: an artificially small budget stops at the right call.

### #10 [P1] Some prompts have no input-size cap
- Location: triage sends up to 60 results with 300-character snippets plus titles and URLs `src/priorart/pipeline.py:146`, `:155–160`; comparisons send up to 9,000 characters of page each `:186`, `:194`; the verifier receives the quotes the comparison returned, with no length limit `:204`, `:224`; the repair call resends up to 6,000 characters `src/priorart/llm.py:112`.
- Risk: Input tokens are billed too. A model that returns a 3,000-character "quote", or a page full of long titles, inflates every downstream call.
- Fix: Cap each element where it enters a prompt: titles at 150 characters, quotes at 300 (`quote_in_page` already needs only a short span), triage at 40 results. Add a final guard in `LLM.json` that measures the serialized payload and refuses anything over a per-task limit with an error rather than sending it.

### #11 [P2] The command line accepts an idea of any size
- Location: `src/priorart/__main__.py:135` (`open(a.file).read()`); the core `run` checks only the minimum length `src/priorart/pipeline.py:266–268`; the 4,000-character limit exists only on the HTTP route `src/priorart/server.py:73`.
- Risk: `python src/agent.py --file big.txt` sends an entire document into the first prompt and pays for it; any future caller of `PriorArt.run` (a worker, another route) inherits no limit.
- Fix: Enforce 10–4,000 characters inside `PriorArt.run` itself (raise `ValueError` with the same message as the API), so every entry point gets the limit; the CLI reports it as a usage error.

## Failure amplification

### #12 [P1] A provider that's failing or out of quota keeps being called
- Location: every check's 14 searches and about 19 model calls are sent regardless of recent failures (`src/priorart/pipeline.py:129–137`, `:301–308`, `:322–330`); quota or billing errors (`402`, `429` with a quota message) are retried like transient ones (`src/priorart/llm.py:92–95`).
- Risk: When a provider is degraded, every check still pays for the calls that succeed before the failure, then ends in an error. When the balance runs out, every check keeps trying until each times out.
- Fix: A per-provider circuit breaker (S.06 #03) that opens immediately on a key, billing or quota error and after 5 failures in 30 seconds. While open, new checks are refused **before any paid call** ("Search is unavailable right now"), the status banner says so, and a P1 alert fires (19.5).

### #13 [P1] The demo outage switch makes paid calls before it fails
- Location: `simulate` accepted from any visitor `src/priorart/server.py:73–77`; the outage wraps only the search client `src/priorart/__init__.py:29–30`, so the decompose model call (`src/priorart/pipeline.py:100`) still runs and is paid for; the selector is public `src/priorart/static/index.html:127`.
- Risk: Anyone can generate paid checks that are guaranteed to fail, and they count against the same budget as real checks.
- Fix: In production, reject `simulate` (S.03 #15). Everywhere else, a simulated run uses the offline demo clients for every stage, so it costs nothing; label such reports as simulated.

## Storage

### #14 [P2] The cache stores whole web pages
- Location: `src/priorart/search.py:56–60` caches `raw_content` for every extracted page in full; the text is cut to 9,000 characters only later `src/priorart/pipeline.py:186`; values are stored uncompressed `src/priorart/cache.py:41–44`.
- Risk: Large pages (documentation sites, long READMEs) are stored at full size, often many times what's ever used, so disk use grows much faster than the number of checks.
- Fix: Store only what the pipeline can use (the first `2 × page_chars` characters), compress values over 4 KB with zlib (a `codec` column), cap any single value at 64 KB, and add the size and age limits from S.04 #12.

### #15 [P2] Saved reports and cache have no quota or retention
- Location: run files kept forever `src/priorart/pipeline.py:404–408`; cache forever `src/priorart/cache.py:24`; nothing checks free disk space.
- Risk: Combined with #01, a script can fill the disk; when it's full, saving fails (S.02 #11) and every check fails after being paid for.
- Fix: Keep at most 200 reports per owner (the oldest move to soft delete, then purge, through `lifecycle.purge_expired`); a retention period for reports with no owner; a disk guard that refuses new checks when free space drops below 10% (readiness reports `degraded`) and alerts at 20%.

### #16 [P2] Keys have no limits set at the providers
- Location: one key per provider for everything (`.env.example:2–3`); no documentation of provider-side spending limits or alerts.
- Risk: The application's own limits (#01–#02) are the only line of defense. A leaked key or an application bug spends without limit, and the provider will keep billing.
- Fix: Separate keys per environment (local, staging, production); set hard monthly or daily spending limits and usage alerts in the Nebius and Tavily consoles where available; a documented rotation procedure. List all of this in `docs/runbooks/out-of-credits.md` (19.5).

## Entitlements, bots and reporting

### #17 [P2] Quotas have no server-side identity to attach to
- Location: no owner or session in the server (`src/priorart/server.py`); Decision A plans anonymous owners (`docs/UX_ARCHITECTURE.md:213`), with the schema ready (`src/priorart/schema.sql:5–8`).
- Risk: Without an owner identity, quotas can only be per IP address, which is unfair behind shared networks (offices, universities, hackathon Wi-Fi) and easy to dodge with rotating IPs.
- Fix: The anonymous owner cookie (S.03 #01); a `usage_daily(owner_id, day, checks, credits, output_tokens)` table updated in the same transaction that starts a check; limits enforced only from that table, never from anything the browser sends. The page shows "3 of 15 checks left today" from a server endpoint. IP-based limits stay as a backstop for clients without the cookie.

### #18 [P2] Nothing distinguishes people from scripts
- Location: the paid endpoint accepts any request `src/priorart/server.py:72–92`.
- Risk: Rate limits slow a determined script down but don't stop a fleet of addresses.
- Fix: A privacy-friendly challenge (Cloudflare Turnstile or hCaptcha), required only after a client's first 2 checks of the day, or for everyone when global spend passes 50% of the daily budget. The server verifies the token with the provider (server-side `siteverify`) before starting work and never trusts a client-side "passed" flag.

### #19 [P2] Payments: when a paid tier is added, design it server-side from the start
- Location: no billing code exists (search above); accounts and paid limits are a recorded follow-up of Decision A (`docs/UX_ARCHITECTURE.md:213`).
- Risk: Payment features added under deadline pressure are where trusting client-side plan flags and replayed webhooks creep in.
- Fix: When a paid tier is added:
  - verify every payment webhook's signature with the provider SDK (for Stripe, `stripe.Webhook.construct_event(payload, sig_header, endpoint_secret, tolerance=300)` on the raw body);
  - record processed event ids in a table with a UNIQUE constraint and skip duplicates (idempotency);
  - derive entitlements only from server-stored subscription state;
  - grant quota changes in the same transaction as the event record;
  - never accept a plan or quota value from the browser.

### #20 [P2] Nobody can see what's being spent
- Location: no usage reporting anywhere (`scripts/` has backup, data, db, seed, submission and query tools only).
- Risk: An abusive pattern, an expensive configuration or a retry storm shows up only when a provider bill or a "quota exceeded" error arrives.
- Fix: `scripts/usage.py`, an offline console tool over the usage table (#06): spend today against the budget; per hour; per provider and model; the top 10 clients by hashed owner or IP; cost per check (median, 95th percentile); cache hit rate and credits saved. Alerts at 50%, 80% and 100% of the daily budget through the 19.5 channel.

## Suggested batches

| Batch | Items | When |
|---|---|---|
| **A: before any public URL** | #01, #02, #03, #04, #13 | Without these, a hosted Prior Art is an open tap on your credits |
| B: count and cap | #06, #07, #08, #09, #10, #11, #12 | Real usage numbers and per-check limits |
| C: stop paying twice | #05 | Normalization, single-flight, attach to running checks |
| D: identity, bots, storage, reporting | #14, #15, #16, #17, #18, #20 | Before launch traffic |
| E: when a paid tier exists | #19 | Not before |

If the app stays local for judging (each judge running it with their own keys), batch A isn't urgent. #04, #08 and #11 still protect the judges' own credits.

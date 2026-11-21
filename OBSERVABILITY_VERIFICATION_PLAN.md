# Prior Art — Observability, Telemetry & Critical-Path Verification (S.09)

Scanned 2026-11-20: logging and error handling in every module under `src/priorart/`, the server's endpoints, configuration loading, the test suite (`tests/`, 161 tests in 7 files) and CI (`.github/workflows/ci.yml`).

## Where things stand (measured today)

- **Logging:** none. No module in `src/priorart/` imports `logging` or creates a logger; the only output is the CLI's 34 `print` calls in `src/priorart/__main__.py`, which are user-facing terminal output, and uvicorn at `log_level="warning"` (`__main__.py:132`).
- **Health:** no liveness or readiness endpoint; `GET /api/status` (`server.py:25–29`) checks nothing and reveals key names.
- **Tests:** 161 passing, 86% line coverage of `src/priorart/`, measured with `coverage run -m pytest` (coverage 7.x installed into this workspace for the measurement only, not added to the project):

| Module | Coverage | Lines not executed by any test |
|---|---|---|
| `pipeline.py` | 91% | 52, 75, 104, 108, 113, 120, 143, 155, 172, 202–203, **234–235**, 241–242, **244–245**, 307–309, 317, 348–349, 369–371 |
| `llm.py` | 85% | 35–37, 53, 66, 88–91, 96, **107–115** |
| `server.py` | 92% | 36, **43–44**, **52**, **87–88** |
| `__init__.py` | 82% | 24–28, 41, 52 |
| `__main__.py` | 0% in-process | run only as a subprocess by `test_template_entrypoint` |
| `report.py`, `search.py`, `queries.py` | 100% | — |
| `static/index.html` (368 lines) | **no tests at all** | — |

The database layer is well tested (schema, migrations, query plans, lifecycle, seed: 95–100%). **The risk is concentrated in the core value loop:** the verifier's labeling branches, the early-failure paths, the server's error paths, and the page.

**Business events that don't exist in Prior Art:** sign-up and payment events. There are no accounts or payments (Decision A, `docs/UX_ARCHITECTURE.md:213`; S.08 confirmed no billing code). Item #04 instruments the events Prior Art does have.

## Logging and telemetry

### #01 [P1] The server and pipeline write no logs
- Location: no `logging` import anywhere in `src/priorart/server.py`, `pipeline.py`, `llm.py`, `search.py`, `cache.py`; uvicorn started at warning level `src/priorart/__main__.py:132`.
- Risk: On launch day there's no record of which checks ran, how long each stage took, which provider failed or why a user saw an error. Every incident starts from nothing.
- Fix (the 19.1 design):
  - `src/priorart/log.py` with a standard-library JSON formatter: `ts`, `level`, `event`, `service`, `env`, `version`, plus context from `contextvars` (`request_id`, `check_id`, `owner_ref` as a keyed hash);
  - request-ID middleware that accepts or creates `X-Request-ID` and returns it on every response;
  - the context copied into the check thread and every pool task (`contextvars.copy_context().run`);
  - one `request.completed` line per request (route template, status, duration), replacing uvicorn's access log, which today would include the idea in the URL;
  - redaction in the formatter: key names (`api_key`, `authorization`, `token`, `idea`, `query`, `prompt`, …) and value patterns (Bearer tokens, `tvly-` keys, email addresses, card-like numbers).
  - The CLI's `print` calls stay as terminal output, routed through one `term.py`; ruff's `T20` rule forbids `print` everywhere else.

### #02 [P1] Failures go to one browser and nowhere else
- Location: the check worker's catch-all `src/priorart/server.py:87–88` (sends `str(e)` to the stream; no test executes it); unreadable run files skipped silently `:43–44` (also never executed by tests).
- Risk: A check that crashes leaves no stack trace, no count and no way to connect a user's report to its cause.
- Fix: `errors.capture(exc, layer="worker", check_id=..., request_id=...)` writes a log line with the traceback and stores a grouped error with a short reference id (19.2). The stream's error event carries the reference, never the raw exception text. Skipped run files are logged once each with the reason. Unexpected 500s go through one FastAPI exception handler that does the same.

### #03 [P1] Provider latency and failures aren't measured
- Location: model calls `src/priorart/llm.py:56–96`; searches `src/priorart/search.py:38–46`; extraction `:52–61`.
- Risk: When checks slow down or fail, there's no way to tell whether it's Token Factory, Tavily or the app, or which model or task is slow. The two-minute check time can't be broken down.
- Fix: Wrap each provider call in `observe_upstream(provider, operation, model)`, which records duration, outcome (`ok`, `timeout`, `rate_limited`, `error`, `cache_hit`), attempt number and usage (#05). It emits a `upstream.call` log line and updates histograms `priorart_upstream_duration_seconds{provider,operation}` and counters `priorart_upstream_requests_total{provider,operation,outcome}` (19.4), exposed on an authenticated `/metrics`.

### #04 [P1] The product's key events aren't recorded
- Location: the pipeline already emits each event to the browser and nowhere else (`self.emit` at `src/priorart/pipeline.py:270–401`); the evidence drawer opens in the page with no record `src/priorart/static/index.html:342–368`.
- Risk: Nobody can answer launch-day questions: how many checks started and finished, how many ended with "no score", how often the checks cut a claim, whether anyone opens the evidence.
- Fix: A typed event catalog (19.4) fed from the same `emit` calls:
  - server events: `check_started`, `check_stage_completed{stage, duration}`, `check_completed{status, novelty, projects, claims_checked, cut_quote, cut_verifier, downgraded, seconds}`, `check_failed{cause}`;
  - client events, sent by beacon: `evidence_viewed`, `source_opened`, `report_downloaded`.

  No idea text, queries or URLs in any event; consent rules from 19.4.

### #05 [P2] Token and credit usage are discarded
- Location: `src/priorart/llm.py:62`, `:69` (only the message text is kept); Tavily `include_usage` is never requested (`src/priorart/search.py:30–35`).
- Risk: Cost per check, per day and per client is unknown, so budget alerts (S.08 #02) and cost dashboards have nothing to read.
- Fix: Record `response.usage` and Tavily usage in `observe_upstream` (#03) and the usage table (S.08 #06); sum per check into the report's `usage` field and the `check_completed` event.

## Health and configuration

### #06 [P1] No liveness or readiness checks
- Location: endpoints `src/priorart/server.py:20–29` (only `/` and `/api/status`; `/api/status` probes nothing and lists missing key names).
- Risk: A host can't tell a healthy instance from one with an unwritable disk, a locked cache or missing keys, so it routes users to broken instances and can't gate deploys.
- Fix (the 19.3 design):
  - `GET /health/live` → `200 {"status":"ok"}`, touching nothing.
  - `GET /health/ready` runs probes in parallel with 500 ms limits:
    - database (`SELECT 1` and migrations at head);
    - runs folder writable (create and delete a temporary file);
    - cache (`SELECT 1`; non-critical, so `degraded`, not failed);
    - configuration valid;
    - providers from recent call outcomes (#03; non-critical; never probed with paid calls).

    It returns `200` (`ready` or `degraded`) or `503 not_ready`, with component **states only**: no paths, versions or key names. Results are reused for 2 s.
  - `/api/status` stops listing key names.

### #07 [P1] Bad configuration fails late and takes everything down
- Location: values read with bare `int(...)` and no bounds `src/priorart/config.py:42–45`; `Settings()` built on every request `src/priorart/server.py:27`, `:34`, `:49`, `:76`; an invalid `PRIORART_SIMULATE` raises inside every check (`src/priorart/__init__.py:41`, a line no test executes); missing keys discovered only when a check starts (`__init__.py:20–23`).
- Risk: A bad deployment starts "successfully" and then fails every request (`ValueError` on a typo) or every check (missing key), with the cause buried in a stack trace.
- Fix: A typed settings schema (pydantic-settings or a validated dataclass) loaded **once at startup**:
  - types and bounds for every variable (`max_candidates` 1–12, `page_chars` 1,000–12,000, `workers` 1–16, `simulate` from the allowed list, `PRIORART_ENV` from development, staging, production);
  - production requires both API keys and refuses fake mode;
  - on failure, exit with code 2 and one line per problem (`PRIORART_MAX_CANDIDATES="eight": must be an integer from 1 to 12`), never printing values of keys.

  `python -m priorart config check` prints the effective settings with keys masked. Tests for every rule.

### #08 [P2] Logs and errors can't be tied to a version
- Location: no `__version__` (`src/priorart/__init__.py`), no build metadata, no `/version`.
- Risk: After a deploy, nobody can tell which version produced an error or whether a fix is live.
- Fix: Version from package metadata (`pyproject.toml`), plus commit and build time from the environment at image build; `GET /version`; an `X-Prior-Art-Version` header on every response; `version` on every log line and error event.

## Tests for the core value loop

### #09 [P0] The verifier-skipped path is untested, and it mislabels claims
- Location: `src/priorart/pipeline.py:233–235` (a claim with no verdict is set to `status="verified"`); lines 234–235 are never executed by the suite.
- Risk: The product's central promise (overlaps are checked) is broken in exactly the branch no test covers: claims the verifier never looked at are shown and exported as verified.
- Fix: First add the test, which should fail against today's code: a fake verifier that returns verdicts for only some claims, asserting the missing ones get `status="unchecked"`, `checks.unchecked` counts them, and the Markdown export labels them "not independently checked". Then fix the code (S.01 #01) so it passes.

### #10 [P1] The downgrade and parsing branches of the verifier never run in tests
- Location: `src/priorart/pipeline.py:243–245` (`downgraded`: never executed), `:241–242` (unparseable `adjusted_score`), `:202–203` (unparseable compare score), `:236` (`supported` given as a string).
- Risk: "Partly" overlaps, a core cell state in the grid, aren't tested, and neither are the parsing paths that decide whether a claim survives (S.01 #02: `"false"` counts as supported).
- Fix: One table-driven test of `verify_one` over verifier replies: `{"supported": true}` → verified; `false` → cut by verifier; `"false"` → cut (fails today); `{"supported": false, "adjusted_score": 1}` on a claimed 2 → downgraded with score 1; `"adjusted_score": "x"` → cut; missing verdict → unchecked (#09). The same for `compare_one`'s score parsing (`"2"`, `2.0`, `"high"`, `null`, `5` → clamped to 2).

### #11 [P1] Early-stop and fallback paths are untested
- Location: `src/priorart/pipeline.py:104`, `:108` (decompose returns no usable features), `:113`, `:120` (no usable queries → fall back to the summary), `:172` (triage returns nothing usable → top search results), `:307–309` and `:317` (a comparison fails / every comparison fails), `:348–349` (no projects → `no_close_match`), `:369–371` (pivots fail).
- Risk: These are exactly the paths a real provider hits on a bad day, and each one decides what the user is told (an honest "no score" versus a crash or a misleading report).
- Fix: A `ScriptedLLM` fake whose replies and exceptions are set per task. One test per path, asserting the final `status`, `message`, the warnings emitted and that nothing raises: for example, every compare raises → `status == "failed"` with "Every comparison failed"; pivots raise → report finishes with `pivots == []` and a warning.

### #12 [P1] The model client's repair and error branches are untested
- Location: `src/priorart/llm.py:107–115` (the repair call), `:35–37` (no JSON found → `LLMError`), `:88–91` (a 400 that means "unknown model"), `:96` (failure after all retries), `:66` (JSON mode turned off after a 400).
- Risk: The repair call (which doubles cost) and the error messages that tell an operator what to fix are unverified; a regression in one of them shows up only in production.
- Fix: Tests with a fake OpenAI transport (`httpx.MockTransport`): a prose reply then valid JSON from the repair (one repair call made, result parsed); a reply with no JSON twice (`LLMError` naming the task); a 400 "model not found" (fails fast with the "set PRIORART_PRIMARY_MODEL" message); three timeouts (`LLMError` after 3 attempts, total sleep bounded).

### #13 [P1] The server's error and security paths are untested
- Location: `src/priorart/server.py:87–88` (worker exception → stream error), `:43–44` (corrupt run file skipped), `:52` (path-traversal guard returns 404), `:36` (no runs folder yet).
- Risk: The traversal guard is the only thing preventing `/api/runs/../../…` from reading files outside the runs folder, and no test proves it; error handling for corrupt files and crashing checks is unverified.
- Fix: FastAPI `TestClient` tests:
  - names `../x.json`, `..%2Fx.json`, `%2e%2e/x.json`, an absolute path, a symlink to a file outside the folder, and `x.txt` → all 404;
  - a truncated JSON file → listed runs skip it and the log records it;
  - a pipeline that raises → the stream ends with an error event and a reference id;
  - a missing runs folder → `[]`.

### #14 [P1] The page has no automated tests
- Location: `src/priorart/static/index.html` (368 lines of rendering and event handling); no browser tests in `tests/`.
- Risk: Everything the user actually sees (the grid, the drawer, the citations, the error banner, Past runs) can break without a single test failing, and the user-facing bugs found in S.02 and S.05 have no regression protection.
- Fix: A Playwright smoke suite (`tests/e2e/`) against a fake-mode server, with Chrome:
  1. Home loads with no console errors;
  2. a sample idea runs to `done`;
  3. the grid has projects × features cells and a "Best coverage" row;
  4. clicking a scored cell opens the drawer with a quote and a source link;
  5. the Markdown download returns `text/markdown` containing the idea;
  6. a past run reopens;
  7. the search-outage demo shows the "no score" banner;
  8. no horizontal overflow at 375 px.

  Runs in CI and saves a trace and screenshots on failure.

### #15 [P1] Report saving has no concurrency test
- Location: `src/priorart/pipeline.py:404–408`.
- Risk: The lost-report bug (128 checks → 31 files in the 20.5 load test) exists because no test saves twice in the same second.
- Fix: A test that calls `save()` 50 times from 8 threads with the same summary and asserts 50 distinct, valid JSON files. It fails today and passes after S.04 #01.

### #16 [P2] The command line isn't tested in-process
- Location: `src/priorart/__main__.py` (128 statements, 0% measured in-process); only a subprocess smoke test exists (`tests/test_pipeline.py`, `test_template_entrypoint`).
- Risk: Argument handling, `--file`, `--md`, `--json`, the `models` command and exit codes on errors are unverified, and coverage can't see them.
- Fix: Call `main([...])` directly with `capsys` and fake mode: a run prints the novelty line and "Saved:"; `--md out.md` writes the file; `--json` output parses; no idea and no `--file` → exit code 2 with the usage message; missing keys → exit 1 with the "Copy .env.example" hint.

### #17 [P1] Nothing checks that the report format matches what the page and exporter read
- Location: the result is built in `src/priorart/pipeline.py:377–399`; the page reads its fields in `src/priorart/static/index.html:273–340` and the exporter in `src/priorart/report.py:19–63`; no shared definition.
- Risk: Renaming or removing one key in the pipeline (`coverage`, `checks`, `gaps`, `sources`) passes every Python test and breaks the report page or the Markdown export in production.
- Fix: A JSON Schema for the report (`docs/contracts/report.schema.json`), validated against every result produced in the pipeline tests and against the saved demo reports; the e2e suite (#14) renders a schema-valid report; CI fails when the schema changes without updating the page tests.

### #18 [P2] No test ever talks to the real providers
- Location: every test uses the fake clients (`src/priorart/fakes.py`) or mocked transports (`tests/test_clients.py`).
- Risk: Changes in the real responses (a Tavily field renamed, a model that ignores JSON mode, a changed error format) are invisible until users hit them, and the submission has never been proven against live keys.
- Fix: An opt-in `pytest -m live` test (skipped without keys) that runs one fixed idea end to end, with the cost caps from S.08 #09, and asserts: a report with status `ok` or `no_close_match`, at least one search returning results, every verified cell's quote found on its page, and per-stage times recorded. Run it before submitting, and nightly once deployed.

### #19 [P2] CI doesn't enforce the checks that matter
- Location: `.github/workflows/ci.yml` (install, SQLite version, `pytest -q`, `scripts/db.py check`); no coverage threshold, lint, type check or browser tests.
- Risk: Coverage can fall, critical branches can lose their tests, and the page can break, all with a green build.
- Fix: Add `coverage run -m pytest` with `fail_under = 85` overall and a separate 100% requirement on the verifier and labeling functions (`verify_one`, `compare_one`, `_finish`) using a coverage include list; add the e2e job (#14), ruff and mypy (20.1).

### #20 [P2] The quality of the core promise isn't monitored after launch
- Location: per-check totals exist only inside each report (`checks` in `src/priorart/pipeline.py:391`); nothing aggregates them over time.
- Risk: If the verifier model starts failing or rubber-stamping, or the quote check starts cutting most claims (a provider changes its page extraction), reports quietly get worse and nobody notices.
- Fix: Metrics from #04's `check_completed`: rates of verified, cut by quote, cut by verifier, downgraded and unchecked claims, verifier failure rate, share of checks ending with no score. Alerts when the verifier failure rate exceeds 10% or unchecked claims exceed 5% over an hour (19.5). A fixed evaluation set of 10 ideas with known overlaps, run weekly with live keys, tracking whether the known projects are found and their quotes verified.

## Suggested batches

| Batch | Items | Notes |
|---|---|---|
| **A: before submission** | #09, #10, #15, #13 | Tests that pin down the core promise and the known bugs; the first three fail today and pass once S.01 #01–#03 are fixed |
| B: the page and the contract | #14, #17 | E2E smoke and a schema for the report |
| C: visibility | #01, #02, #03, #04, #08 | Logs, errors, provider timings, business events, version |
| D: deploy safety | #06, #07 | Health checks and startup validation |
| E: depth | #05, #11, #12, #16, #18, #19, #20 | Remaining branches, live smoke, CI gates, quality monitoring |

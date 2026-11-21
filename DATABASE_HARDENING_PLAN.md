# Prior Art — Database, Schema Integrity & Query Scale (S.04)

Scanned 2026-11-20: the schema (`src/priorart/schema.sql`, 422 lines), migrations `0001`–`0004`, the connection layer (`src/priorart/db.py`), every named query (`src/priorart/queries.py`), the lifecycle code (`src/priorart/lifecycle.py`), the API cache store (`src/priorart/cache.py`), and every place the app actually persists data (`src/priorart/pipeline.py`, `src/priorart/server.py`).

**The most important fact: the app has two persistence layers, and only the weaker one is in use.**

1. **What runs today:** each check is saved as one JSON file in `runs/` (`pipeline.py:404–408`) and read back by listing the folder (`server.py:32–58`). The API cache is a second SQLite file (`cache.py`).
2. **What is built but not connected:** a 03.x SQLite schema that is already strong:
   - STRICT tables;
   - composite `(report_id, owner_id)` foreign keys with cascades on every child table;
   - NOT NULL/CHECK bounds on every column;
   - UNIQUE keys for dedupe (`candidates (report_id, url_key)`, `overlaps (project_id, feature_id)`);
   - triggers that make finished reports immutable and enforce finish rules;
   - indexes for every hot query and every foreign-key lookup, verified by `tests/test_query_plans.py`;
   - every list query has a LIMIT and keyset pagination (`queries.py`);
   - lifecycle writes use `BEGIN IMMEDIATE` with a `foreign_key_check` before commit (`lifecycle.py:35–50`).

So most items below are either about the live JSON and cache stores, or about **wiring the strong layer in without losing its guarantees.** There are no counters or balances in the data model; the race conditions that exist are on report state transitions and on paid API calls.

Baseline: `python -m pytest -q` → 161 passed; `python scripts/db.py check` → passed (0004 of 0004).

## The persistence in use today

### #01 [P0] Saved reports overwrite each other and can be left half-written
- Location: `src/priorart/pipeline.py:404–408` (name `%Y%m%d-%H%M%S-<slug>`, `path.write_text(...)` straight to the final path).
- Risk: Two checks that finish in the same second with similar summaries write the same file. In today's load test, 128 finished checks left 31 files. A crash or full disk mid-write leaves truncated JSON that `/api/runs` silently skips (`server.py:43–44`), so the report vanishes.
- Fix: Until #03 lands: name files `<YYYYmmdd-HHMMSS>-<secrets.token_hex(4)>.json`, write to a temporary file in the same folder, `flush()` + `os.fsync()`, then `os.replace()`. Test: 50 saves in the same second produce 50 valid files.

### #02 [P1] Every Home load scans the whole runs folder and parses 30 full reports
- Location: `src/priorart/server.py:35–44` (`sorted(d.glob("*.json"), reverse=True)[:30]`, then `json.loads(p.read_text())` on each).
- Risk: The listing is unbounded: it stats and sorts every file ever saved on every request, then reads 30 complete reports (every project, cell and quote) to show 30 one-line summaries. With thousands of runs, Home and every check's completion (`index.html:261` reloads the list) slow down linearly, all inside the request.
- Fix: Serve the list from the database (`queries.recent_reports`, already indexed by `reports_owner_recent`, `schema.sql:183`, LIMIT 20). If the JSON store must stay for a release, maintain a small append-only `runs/index.jsonl` of `{file, summary, status, novelty, created}` written in the same atomic step as the report, and read only its last 30 lines.

### #03 [P1] Reports never reach the database
- Location: writes `src/priorart/pipeline.py:400–408`; reads `src/priorart/server.py:32–65`; unused layer `src/priorart/db.py:53–102`, `src/priorart/queries.py`, `src/priorart/lifecycle.py`.
- Risk: None of the constraints, ownership, cascades, soft delete, audit or backup guarantees apply to real data. Backups (`scripts/backup.py`) back up a database the app never writes to.
- Fix: A `ReportStore` built on one process-wide `Pool`, created in the FastAPI lifespan and closed on shutdown (never a connection per request), with one short `BEGIN IMMEDIATE` transaction per pipeline stage:
  1. **start:** insert `reports` (status `running`) + event 1;
  2. **plan:** features, search queries;
  3. **search:** candidates (dedupe through `UNIQUE (report_id, url_key)` with `INSERT ... ON CONFLICT DO NOTHING`, not a read-then-insert);
  4. **compare/verify:** projects, overlaps;
  5. **finish:** pivots, then `UPDATE reports SET status=?, ... WHERE id=? AND status='running'`, checking `rowcount == 1`.

  Each stage also appends its events in the same transaction. A failure at any stage records `status='failed'` with a message in its own transaction. Integration test: a full check through the server is readable through `queries.report_by_id` and the child queries, and the finish trigger passes.

## Constraints and validation

### #04 [P1] Model output can exceed the schema's bounds, so finishing a report would abort
- Location: schema bounds `src/priorart/schema.sql:19` (summary ≤ 500), `:53` (feature name ≤ 120), `:68` (query text ≤ 390), `:92` (snippet ≤ 600), `:108` (project name ≤ 200), `:158` (pivot title ≤ 120); pipeline values with no clamp: `src/priorart/pipeline.py:105` (feature names), `:120–121` (the summary falls back to the full idea, up to 4,000 characters, and so do fallback queries), `:260` (pivots).
- Risk: As soon as #03 is wired, one long model answer, or a model that returns no summary, makes an insert hit a CHECK constraint and the whole two-minute, paid check fails at save. The JSON store accepts anything, so this is invisible today.
- Fix: One mapping layer (`to_rows(result)`) that trims and truncates every text field to its column bound, on a word boundary with "…", and drops empty names. Generate the bounds from `schema.sql` at import time so they can't drift, and add a property test that feeds over-long and empty values for every field and asserts the insert succeeds.

### #05 [P2] The API accepts ideas the database will reject
- Location: `src/priorart/server.py:73` (`min_length=10` on the raw string) against `src/priorart/schema.sql:18` (`length(trim(idea)) BETWEEN 10 AND 4000`).
- Risk: An idea like `"   short   "` passes the API, starts work, and then fails at the first insert with a 500 instead of a clear validation message.
- Fix: Strip whitespace in the request model before applying the 10–4,000 bound (S.03 #12), and a test asserting that every value the API accepts can be inserted.

### #06 [P2] Duplicate submissions create duplicate paid checks
- Location: `src/priorart/schema.sql:14–46` (`reports` has no request-level uniqueness); start path `src/priorart/server.py:72–92`; button `src/priorart/static/index.html:236–247`.
- Risk: A double-click, a retried request after a dropped connection, or a client retry on timeout starts the same check twice and spends twice the credits.
- Fix: Migration `0005` (expand): `ALTER TABLE reports ADD COLUMN client_request_id TEXT` plus `CREATE UNIQUE INDEX reports_idempotency ON reports (owner_id, client_request_id) WHERE client_request_id IS NOT NULL`. The page sends a UUID per **Check idea** press; the server inserts with `ON CONFLICT DO NOTHING` and returns the existing report's id when it already exists.

### #07 [P1] A query leaks other owners' reports through pivot links
- Location: `src/priorart/queries.py:90–93` (`pivots_of_report`: `WHERE pivot_of_report_id = ? AND deleted_at IS NULL`, no `owner_id`); foreign key `src/priorart/schema.sql:17` lets any report point at any other.
- Risk: When owner B checks a pivot from A's shared report, A's report page lists B's report (summary, status, novelty) under "Checked as a pivot", exposing another person's work.
- Fix: Add `AND owner_id = ?` (the viewer's owner id) to `pivots_of_report`; replace partial index `reports_pivot_of` with `(pivot_of_report_id, owner_id) WHERE pivot_of_report_id IS NOT NULL`, migration `0005`; extend `tests/test_query_plans.py` and add a cross-owner test.

## Transactions, state and recovery

### #08 [P1] The write connection can be handed out with a stale open transaction
- Location: `src/priorart/db.py:77–84` (`write()` releases the lock without checking `in_transaction`), compared with `read()` at `:87–96`, which rolls back.
- Risk: Any caller that raises between its own `BEGIN` and `COMMIT` without using `lifecycle.transaction` leaves the single shared write connection inside a transaction. The next request's `BEGIN IMMEDIATE` fails with "cannot start a transaction within a transaction", or worse, its `COMMIT` commits the failed caller's partial writes. With one writer, this stalls every write in the app.
- Fix: In `write()`'s `finally`: `if self._writer.in_transaction: self._writer.execute("ROLLBACK")` before releasing the lock, and log it. Test: a writer that raises mid-transaction leaves the next writer a clean connection and no partial rows.

### #09 [P1] Reports stuck in "running" after a crash can never finish or be deleted
- Location: status rules `src/priorart/schema.sql:21–22`, `:38`; `reports_running_not_deletable` `:377–379`; checks run in daemon threads that die with the process (`src/priorart/server.py:91`).
- Risk: Once #03 is wired, every restart, deploy or crash during a check leaves a `running` report forever. Its page spins indefinitely, the owner can't delete it (the trigger forbids deleting running reports), and it never ages out through purge.
- Fix: A `heartbeat_at` column (migration `0005`) updated with each stage write, and a partial index `reports_running ON reports (heartbeat_at) WHERE status = 'running'`. On startup and every 5 minutes: `UPDATE reports SET status='failed', status_message='Interrupted: the server restarted during this check', finished_at=now WHERE status='running' AND heartbeat_at < now - 10 minutes`. Test the recovery with a simulated crash.

### #10 [P2] Concurrent checks of the same idea pay for the same API calls twice
- Location: `src/priorart/cache.py:36–45` (the lock covers the lookup and the insert, not `fn()` in between).
- Risk: Two people checking the same sample idea at once (common during a demo or judging) both miss the cache and both make about 12 searches and 20 model calls. Under a burst, credit use multiplies by the number of simultaneous identical requests.
- Fix: Single-flight per key: a dictionary of in-progress keys to `threading.Event` (or a `Future`); the first caller computes and stores; others wait on the event and read the stored value. Waiting callers time out to computing themselves. Test with 10 threads and one key: `fn` runs once.

## The cache database

### #11 [P1] The cache database will lock up under concurrent checks
- Location: `src/priorart/cache.py:21–25` (a new `sqlite3.connect` per `Cache`, default rollback journal, Python's default 5 s lock timeout); one `Cache` per check `src/priorart/__init__.py:27`; a commit per write `cache.py:41–44`. Fake mode doesn't use the cache (`__init__.py:13–16`), so the load test never exercised this.
- Risk: Several live checks write to the same file through separate connections without WAL; writers block readers, and contention raises `sqlite3.OperationalError: database is locked` inside a search or model call, which fails that stage of the check. Each check also leaks an open connection (no `close()`).
- Fix: One `Cache` per process (opened at startup, closed at shutdown) with `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`; treat any cache error as a miss (log it, call the API) so the cache can never fail a check. Test: 8 threads hammering get/put on a file-backed cache see no errors.

### #12 [P2] The cache grows without bound and can't be expired
- Location: `src/priorart/cache.py:24` (`kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)`: no timestamp or size); values include full extracted page text (`src/priorart/search.py:58`, `raw_content`).
- Risk: Every page ever read is stored forever. The file grows with every new idea, never shrinks, and stale results are served indefinitely (S.02 #12) with no column to expire them by.
- Fix: Recreate as `kv (k TEXT PRIMARY KEY, kind TEXT NOT NULL, v TEXT NOT NULL, created_at INTEGER NOT NULL, last_used_at INTEGER NOT NULL, bytes INTEGER NOT NULL)` with an index on `(kind, created_at)`. Expire per kind (search/extract 24 h, model answers 7 days), cap total size with LRU eviction by `last_used_at`, and run `PRAGMA incremental_vacuum` (`auto_vacuum=INCREMENTAL`) during a daily job.

## Growth and retention

### #13 [P2] The progress log is never compacted
- Location: `src/priorart/schema.sql:169–181` (`report_events`, one row per emitted event, append-only); open item `docs/DATA_MODEL.md:252`.
- Risk: Every check adds dozens of event rows whose payloads repeat table data. They're needed only to replay a running check, but they stay forever and come with every export.
- Fix: A daily job (one `BEGIN IMMEDIATE` batch of up to 500 reports) that deletes `report_events` for reports finished more than 7 days ago, keeping the `done` event. Deletes are allowed (the trigger blocks only updates and inserts after finish). A test asserts a finished report still renders entirely from the tables.

### #14 [P2] The audit trail can only grow
- Location: `src/priorart/schema.sql:354–375` (`audit_events`; `audit_events_no_delete` at `:374`); open item `docs/DATA_LIFECYCLE.md:201`.
- Risk: No retention period exists and the trigger forbids deletion, so the table grows without limit.
- Fix: Choose a retention period (proposed: 400 days). A migration replaces `audit_events_no_delete` with a trigger that allows a delete only when `created_at` is older than that period and `audit_context.actor = 'purge'`; the daily purge removes expired entries in batches.

### #15 [P2] Anonymous owners pile up
- Location: `src/priorart/schema.sql:5–12` (`owners`: no last-seen time); owners are created per browser (Decision A, `docs/UX_ARCHITECTURE.md:213`).
- Risk: Once the owner cookie (S.03 #01) ships, every crawler, bot and private-browsing visit creates an owner row that never goes away.
- Fix: Create the owner row lazily, only when a browser starts its first check; add `last_seen_at` (migration `0005`), updated at most once a day per owner; purge owners with no reports that haven't been seen for 30 days.

## Query efficiency

### #16 [P2] Export runs several queries per report and holds one snapshot throughout
- Location: `src/priorart/lifecycle.py:99–117` (`_report_export`: one report query, five child queries and paged event queries per report), called in a loop `:130–135`, all inside one read transaction `:125–148`.
- Risk: An owner with hundreds of reports triggers thousands of queries, and the long read snapshot stops WAL checkpoints from completing, so the WAL file grows for the whole export.
- Fix: Read each table once for the owner (`WHERE owner_id = ?`, keyset-paged, using a new `(owner_id, report_id)` index or via a join on `reports`), group rows by `report_id` in Python, and stream the export as JSON lines so no single snapshot lasts longer than one page.

### #17 [P2] Deleting an owner runs full-table counts inside the write lock
- Location: `src/priorart/lifecycle.py:156–171` (`SELECT COUNT(*) FROM {t} WHERE owner_id = ?` on 8 tables, twice); the child-table indexes all start with `report_id` (`schema.sql` composite keys; `FK_LOOKUPS`, `queries.py:120–134`), so `owner_id` alone can't use them.
- Risk: Sixteen scans of every child table run while holding the database's only write lock. As the database grows, every other write in the app waits for them.
- Fix: Count through the owner's reports (`WHERE report_id IN (SELECT id FROM reports WHERE owner_id = ?)`, which uses the existing indexes), or use `total_changes()` before and after the delete. Add the case to `tests/test_query_plans.py` so it stays indexed.

## Durability and operations

### #18 [P1] Backups are on the same disk and only run if someone schedules them
- Location: `scripts/backup.py:3`, `:8` (default folder `data/backups/`, next to the database); schedule documented only as a manual cron line (`docs/DATA_LIFECYCLE.md` §4.3).
- Risk: A lost or corrupted volume takes the database and every backup with it. On a new host nobody adds the cron line, so there are no backups at all.
- Fix: Continuous replication of the SQLite file off the host with Litestream (WAL streaming to object storage, point-in-time restore), or the existing hourly `backup.py` plus an upload step to object storage with server-side encryption. Schedule it in the deployment config, and alert when the newest off-host backup is more than 2 hours old.

### #19 [P2] Connection settings leave performance and file growth to defaults
- Location: `src/priorart/db.py:42–44` (sets `foreign_keys`, `journal_mode=WAL`, `busy_timeout` only).
- Risk: In WAL mode the default `synchronous=FULL` syncs the disk on every commit, which matters once each stage of every check is a transaction (#03). The WAL file has no size limit after checkpoints, and query-planner statistics are never refreshed.
- Fix: Add `PRAGMA synchronous = NORMAL` (safe with WAL: a power loss can lose the last transaction but never corrupts), `journal_size_limit = 67108864`, `temp_store = MEMORY`, a 64 MB `mmap_size`; run `PRAGMA optimize` when the pool closes and daily; run `PRAGMA wal_checkpoint(TRUNCATE)` in the daily job. Measure commit latency before and after with the 03.3 harness.

### #20 [P2] The live database's integrity is never checked
- Location: `src/priorart/lifecycle.py:174–186` (`verify()` runs `integrity_check` and `foreign_key_check`, but only on backup files); nothing runs at startup or on a schedule.
- Risk: Corruption from a bad disk or a hand-run SQL statement goes unnoticed until a query fails, and every backup taken in the meantime copies the damage.
- Fix: Run `PRAGMA quick_check` and `PRAGMA foreign_key_check` at startup, and refuse readiness if either fails. Add a daily `integrity_check` job that alerts on any result other than `ok`. Backups record the check result and refuse to rotate out the last good backup while the live database is failing.

## Suggested batches

| Batch | Items | When |
|---|---|---|
| **A: today, on the live JSON store** | #01, #02 | Stops report loss before judging |
| B: the cache | #11, #10 | Before any traffic with real keys |
| C: wiring the database | #03, #04, #05, #07, #08, #09, migration 0005 for #06 | One connected change |
| D: retention and growth | #12, #13, #14, #15 | After C |
| E: operations | #16, #17, #18, #19, #20 | Before a hosted launch |

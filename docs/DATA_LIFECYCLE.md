# Prior Art — Data Lifecycle

Phase 03 · Prompt 03.5 · How every record is created, changed, deleted, recovered and backed up.

## 0. Basis and conflicts

- **Builds on** the schema (`docs/DATA_MODEL.md`), migrations (`docs/MIGRATIONS.md`), queries (`docs/QUERY_PLAN.md`) and seed (README "Seeded data").
- **Implemented as:**
  - migration `migrations/0004_lifecycle`;
  - `src/priorart/lifecycle.py`;
  - the connection `Pool` in `src/priorart/db.py`;
  - `scripts/backup.py` and `scripts/data.py`.
- **There is no deployment target.** Prior Art runs locally (`python src/agent.py serve`), and the repo has no hosting or deploy configuration. So "automated backups on the deployment target" means a schedule on the machine that runs it: §4.3 gives the exact line for macOS. The restore drill was performed and timed for real (§4.2).
- **The audit "who" is pseudonymous.** There are no accounts (`docs/DATA_MODEL.md` §0). Who = the actor role (owner, operator, purge, seed, system, or "sql" for hand-run SQL) plus the owner's internal number.
- **Two 03.1 schema bugs** were found and fixed in 0004:
  1. `report_events_append_only` blocked every update to events, including the `owner_id` cascade. A report with a progress log could never change owner. Now it rejects only real changes to an event's content.
  2. Finished reports are read-only (I8). Soft delete needed one exception: `deleted_at` isn't in the locked column list, so it can be set on a finished report. Every other column stays locked.

## 1. Deletion policy per table

| Table | Policy | How | Cascade that matches it |
|---|---|---|---|
| **reports** | **Soft delete → hard delete after 30 days** | Owner deletes: `deleted_at` is set and the report is hidden at once. Restorable for `RETENTION_DAYS = 30` (`lifecycle.py:22`). Then `purge_expired` deletes the row. | Purge deletes the report row; every child table cascades from it |
| features, search_queries, candidates, projects, overlaps, pivots, report_events | **Never deleted on their own** | They're parts of a report: kept while it's soft-deleted (so restore is complete) and removed with it | `ON DELETE CASCADE` from `reports (id, owner_id)` |
| **owners** | **Hard delete, immediate** ("delete my data") | `delete_owner` (`lifecycle.py:156`). Offered with an export first. | `ON DELETE CASCADE` to reports, and from there to every child table |
| reports.pivot_of_report_id | Reference cleared | When the parent report is purged, the pivot report stays and loses its link | `ON DELETE SET NULL` |
| **audit_events** | **Kept; holds no content** | Append-only: updates and deletes are refused by triggers. Holds only ids, statuses and timestamps, never idea text or links (`test_audit_holds_no_content`). | None on purpose: `owner_ref` and `report_ref` aren't foreign keys, so the trail survives the rows it describes |
| audit_context | Single row, never deleted | Records who is acting during a transaction | — |

**Why soft delete only for reports:**
- A report is the thing people delete by accident, and it's what a restore window protects.
- Owners are deleted only on an explicit privacy request, which must be immediate and complete.
- Child rows have no meaning outside their report.

**Rules the database enforces:**
- A running check can't be deleted. Trigger `reports_running_not_deletable` (`0004_lifecycle.up.sql:53`) rejects it, because the pipeline is still writing to it.
- `deleted_at` can't be earlier than `created_at`.

### Queries respect soft delete everywhere

- Every query in `src/priorart/queries.py` that reads `reports` includes `deleted_at IS NULL`, except the four lifecycle queries marked `includes_deleted`:
  - `recently_deleted`;
  - `purge_due`;
  - `owner_reports_page`, used by export;
  - `owner_audit_page`, which reads audit entries rather than reports.
- `test_every_reports_query_respects_soft_delete` fails if a query breaks that rule.
- Per-report child queries run only after `report_by_id` has returned the report. A deleted report returns nothing, so its parts are never read through the app (`test_soft_delete_hides_from_every_app_query_and_restore_brings_it_back`).

## 2. Audit trail

Table `audit_events` (`0004_lifecycle.up.sql:21`) records:
- **who**: the actor;
- **what**: the action, `owner_ref` and `report_ref`;
- **when**: `created_at`, UTC;
- **before / after**: JSON objects.

Triggers write it, so even a change made with hand-run SQL is recorded, attributed to `sql`:

| Action | Recorded by | Before → after |
|---|---|---|
| `report_deleted` | trigger on `deleted_at` set | `{"deleted_at": null}` → `{"deleted_at": "<when>"}` |
| `report_restored` | trigger on `deleted_at` cleared | `{"deleted_at": "<when>"}` → `{"deleted_at": null}` |
| `report_purged` | trigger after a report row is deleted (by purge or owner deletion) | `{"status", "created_at", "deleted_at"}` → none |
| `owner_reassigned` | trigger on `reports.owner_id` change (e.g. a future account claiming anonymous reports) | `{"owner_id": old}` → `{"owner_id": new}` |
| `owner_deleted` | trigger before an owner row is deleted | `{"reports": count, "created_at"}` → none |
| `owner_exported` | `export_owner` | none → `{"reports": count}` |

`lifecycle.transaction` (`lifecycle.py:35`) sets the actor in `audit_context` inside the same transaction as the change, then resets it. Only one writer runs at a time (§6), so the actor can't leak between changes.

## 3. Privacy flows

### Export: "give me my data"

`python scripts/data.py export --owner-token <token> --out my-data.json` (or `export_owner`, `lifecycle.py:120`) writes one JSON file with:
- the owner;
- every report, deleted-but-restorable ones included, each with its features, searches, projects and sources, every overlap with its quote and check verdict, pivots and the full progress log;
- the audit entries about them.

Details:
- It reads from one consistent snapshot: the app can keep writing during an export.
- It pages through reports by (created_at, id), so reports with identical timestamps aren't skipped (`test_export_pages_through_large_owners_without_losing_reports`, 230 reports).
- The export itself is logged.

### Deletion: "delete my data"

`python scripts/data.py delete-owner --owner-token <token> --yes` (or `delete_owner`):
- **What it does:** removes the owner and every row they own, in one transaction. Inside that transaction it also counts what's left and rolls back if anything survived.
- **What remains:** audit entries with the owner's former internal number, actions and timestamps: no idea text, links or token.
- **Confirmation:** without `--yes` it explains and stops.
- **Export first:** the CLI says to export first if the person wants a copy.

### Retention

`python scripts/data.py purge` permanently removes reports soft-deleted more than 30 days ago:
- 200 at a time (`purge_due`, served by the partial index `reports_purge_due`);
- each batch is its own short transaction, so the app keeps working.

Run it daily (§4.3).

## 4. Backups and restore

### 4.1 How backups work

`python scripts/backup.py backup` (`lifecycle.py:189`):
- **Copy:** SQLite's online backup API makes a consistent copy while the app keeps running.
- **Verify:** the copy is then checked with `PRAGMA integrity_check`, `PRAGMA foreign_key_check` and its schema version.
- **Where:** files go to `data/backups/priorart-<UTC time>.sqlite` (git-ignored).
- **Retention:** the newest 48 are kept (`--keep`).

`backup.py list` shows each file's size, schema version and integrity.

### 4.2 Restore drill: performed and timed

**Drill 1, full volume** (`python scripts/backup.py drill`). It builds a database with migrations 0001–0004, the seed, and 1,000 owners × 4 reports (4,000+ reports, 120,000 grid cells). Then it backs up, deletes the database, restores, and compares a SHA-256 over every row of every table:

```
1. database built: 56.1 MB, fingerprint b6d7c98c3ff8278a…
2. backup taken and verified: 56.1 MB in 0.12s
3. database deleted (simulated loss): exists=False
4. restored: copy 0.11s, integrity ok, schema 0004
5. fingerprint after restore b6d7c98c3ff8278a… MATCHES
restore time (restore + verify + fingerprint): 2.15s
```

**Measured restore time for a 56 MB database: 0.11 s to copy, 2.15 s including full verification.** The data was identical after restore.

**Drill 2, the real local database**, using the exact steps in §4.4 (2026-11-20, schema 0004, seeded):

```
$ python scripts/backup.py backup
backup data/backups/priorart-20261120T075444Z.sqlite  0.7 MB  schema 0004  integrity ok  0.00s
fingerprint before 7e97013c33803460
$ rm data/priorart.sqlite*                      # simulated loss
$ time python scripts/backup.py restore data/backups/priorart-20261120T075444Z.sqlite --yes
restored data/priorart.sqlite from data/backups/priorart-20261120T075444Z.sqlite in 0.00s: schema 0004, integrity ok
real 0m0.079s
fingerprint after  7e97013c33803460
$ python scripts/db.py status
current version: 0004 of 0004
```

### 4.3 Automated schedule

There's no server to configure, so the schedule runs on the machine that hosts Prior Art. On macOS or Linux, `crontab -e`, replacing the path with yours:

```
0 * * * *  cd /path/to/prior-art && /usr/bin/env python3 scripts/backup.py backup --keep 48 >> data/backup.log 2>&1
30 3 * * * cd /path/to/prior-art && /usr/bin/env python3 scripts/data.py purge >> data/purge.log 2>&1
```

That's hourly backups with 48 kept (two days), and a daily purge. For anything beyond one machine, copy `data/backups/` off-machine as well (Follow-ups).

### 4.4 Exact restore steps

1. **Stop the app** (Ctrl-C the `python src/agent.py serve` process), so nothing writes during the restore.
2. **Pick the backup:** `python scripts/backup.py list`. Choose the newest one showing `integrity ok`.
3. **Keep the damaged file** if you may need it: `mv data/priorart.sqlite data/priorart.damaged.sqlite`. Remove `data/priorart.sqlite-wal` and `-shm` if present.
4. **Restore:** `python scripts/backup.py restore data/backups/<file>.sqlite --yes`. It verifies the backup first, copies it in, then verifies the result.
5. **Check:** `python scripts/db.py status`. It should report the expected schema version with nothing pending. If the backup predates a migration, run `python scripts/db.py up`.
6. **Start the app again.** Anything written after the backup's timestamp is lost; with hourly backups that's at most one hour.

## 5. Orphan prevention: proof

**Static** (`test_every_foreign_key_cascades_or_nulls`):
- every one of the schema's foreign keys is `ON DELETE CASCADE` or `ON DELETE SET NULL` on a nullable column;
- none is `NO ACTION`/`RESTRICT`;
- foreign keys are on for every connection, pool connections included (`db.py:27`).

With both true, deleting any parent either deletes its children or clears a reference, and no path leaves a child pointing at a missing parent.

**Dynamic** (`test_no_lifecycle_path_leaves_orphans`, 5 cases). Each lifecycle path is run on the full seed:
- soft delete;
- purge of a report that other reports pivot from;
- owner deletion;
- re-seed;
- owner reassignment.

After each, `PRAGMA foreign_key_check` must be empty, and a join confirms every child's report exists with the same owner.

**Defense in depth:** every `lifecycle.transaction` runs `PRAGMA foreign_key_check` before commit and rolls back otherwise. `test_a_lifecycle_change_that_would_orphan_is_rolled_back` proves it with foreign keys switched off on purpose.

## 6. Connection pooling

Deployment model: one long-lived local server process (`uvicorn`). Each check runs in its own thread (`src/priorart/server.py:92`). It isn't serverless.

| Connection | Count | Why |
|---|---|---|
| Write | **1**, behind a lock (`db.py:78`) | SQLite allows one writer per file at a time; more write connections would only queue on SQLite's file lock |
| Read | **4** by default (`PRIORART_DB_READERS`), `PRAGMA query_only` (`db.py:74`) | WAL lets reads run in parallel with the writer. Hot reads take 2–30 µs (`docs/QUERY_PLAN.md` §4), so 4 serve far more page loads than one local server sees. A read connection can't write by mistake. |
| Timeout | 5 s, then `PoolTimeout` | A request fails clearly instead of hanging |

`test_pool_one_writer_parallel_read_only_readers` runs 6 reader threads and 2 writer threads at once. It checks that every write lands with no errors, that readers can't write, and that the pool times out when exhausted.

If Prior Art is ever deployed serverless, this changes: per-invocation connections and a server database (PostgreSQL with an external pooler). SQLite files don't fit that model.

## Follow-ups (out of scope for this phase)

- **The app doesn't use the database yet.** Wiring `Pool`, the queries, and delete/restore/export endpoints into `server.py` belongs to the storage phase that replaces JSON files (`pipeline.py:404–408`).
- **The owner cookie** (DATA_MODEL Follow-ups) is needed before owners can export or delete their own data from the web page; today only the operator scripts can.
- **Off-machine backup copies** (another disk or cloud storage) once anything beyond local use exists.
- **Audit retention:** audit rows can't be deleted today (by design). Set a retention period, then add an operator-run migration that removes entries older than it.
- **Restore time at real scale:** re-run `backup.py drill --owners N` once real usage gives a size to plan for. The measured rate here is about 0.11 s per 56 MB to copy, plus verification.

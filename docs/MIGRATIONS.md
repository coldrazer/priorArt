# Prior Art — Migrations

Phase 03 · Prompt 03.2 · How the database schema changes, and the rules for writing a safe migration.

**The one rule:** every schema change is a numbered, reviewed, reversible migration file in `migrations/`. Nobody edits a database by hand, and nobody edits a migration after it has been applied anywhere.

## 1. How it works

| Piece | Where | What it does |
|---|---|---|
| Migration files | `migrations/NNNN_snake_name.up.sql` plus either `.down.sql` or `.irreversible.md` | The change and how to undo it. The file name pattern is enforced (`src/priorart/migrate.py:24`). |
| Runner | `src/priorart/migrate.py` | Applies or reverses migrations against a connection from `src/priorart/db.py:24`, which turns on foreign keys (`:38`) and WAL (`:40`) |
| History table | `schema_migrations` (`migrate.py:28`) | One row per applied migration: version, name, SHA-256 of the up file, applied time (UTC) |
| Model definition | `src/priorart/schema.sql` | The full current schema in one readable file. The CI check fails if the migrations don't produce exactly this. |
| Local commands | `scripts/db.py` | `status`, `up [--to N]`, `down [--steps N \| --to N]`, `reset --yes`, `check` |
| CI | `.github/workflows/ci.yml` | Runs the tests and `python scripts/db.py check` on every push and pull request |

The database file is `data/priorart.sqlite` (git-ignored). Override it with `--db` or `PRIORART_DB_PATH`.

### What the runner guarantees

- **Deterministic order.** Versions are 4-digit numbers and must run 0001…N with no gaps or duplicates (`migrate.py:103`). Two people adding "0005" at once is caught: the second one has to renumber.
- **All or nothing.** Each migration runs in one `BEGIN IMMEDIATE` transaction together with its history row (`migrate.py:229`). Any error rolls back the schema change and the history row.
- **No dangling references.** `PRAGMA foreign_key_check` runs before commit (`migrate.py:233`); a migration that breaks a foreign key is rolled back.
- **History can't be rewritten silently.** Before any command, every applied migration's file must still exist with the same name and the same checksum (`migrate.py:169`). If someone edits an applied migration, every command stops until the file is restored.
- **Irreversible is explicit.** `down` refuses to pass an irreversible migration and rolls back nothing at all (`migrate.py:215`). It prints the path to the recovery plan.
- **Files can't take over the connection.** A migration may not contain `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`, `PRAGMA`, `VACUUM` or `ATTACH` (`migrate.py:39`). The runner owns transactions and connection settings.
- **Large tables are protected** (§4).

## 2. Writing a migration

1. Find the next number: `python scripts/db.py status`.
2. Create `migrations/NNNN_what_it_does.up.sql`. Use lowercase snake_case, saying what changes, not why.
3. Create `NNNN_what_it_does.down.sql` that undoes it exactly. Undo in reverse order: last thing created, first thing dropped.
4. Apply the same change to `src/priorart/schema.sql`, so the model shows the schema as it now is. New columns go **at the end of the table's column list**, before its constraints: that's where SQLite's `ALTER TABLE ADD COLUMN` puts them, so the drift check can compare like with like.
5. Run `python scripts/db.py check` and `python -m pytest -q`. Both must pass.
6. If the change affects an invariant, update `docs/DATA_MODEL.md` §6 and `tests/test_schema.py` in the same change.

**Never:**
- edit or delete a migration that has been applied anywhere, including your teammate's laptop: write a new one;
- change a version number after merge;
- put data fixes for one environment in a migration.

### Down migrations

- A down file must leave the schema exactly as it was before the up file: the CI check proves it by going to zero and back.
- If the up file destroys information (dropping a column or table, lossy type changes, merging rows), the down file can recreate the *structure* but not the *data*. Then the migration is irreversible: see §5.
- `SELECT 1;` is a valid down file only when the up file changes no schema (for example, a batched backfill whose old values are still derivable).

## 3. Breaking changes: expand → backfill → switch → contract

Old and new application code must be able to run against the same database during a deploy. So a breaking change (renaming a column, changing a type, splitting a table) is never one migration. It's four steps, each shipped separately:

| Step | Migration | Application code | Reversible? |
|---|---|---|---|
| **1. Expand** | Add the new column or table, nullable or with a default. Add triggers that copy writes from old to new if both versions will write. | Old code keeps working; it doesn't know about the new column. | Yes: drop what was added |
| **2. Backfill** | Fill the new column from the old one **in batches** (§4), e.g. `UPDATE t SET new = old WHERE id BETWEEN 1 AND 5000;` repeated per range. | Unchanged | Yes: the old column is untouched |
| **3. Switch** | Usually none; sometimes add the `NOT NULL` or `CHECK` on the new column once it's fully populated. | New code reads and writes the new column. Deploy, watch, keep the old column. | Yes: old code still works |
| **4. Contract** | Drop the old column, old triggers and old indexes. | Only new code is running. | **Usually irreversible**: needs a `.irreversible.md` (§5) |

Example, renaming `projects.one_liner` to `projects.summary`:
- 0007 adds `summary` and a trigger copying `one_liner` writes;
- 0008 backfills in batches;
- the app switches to `summary`;
- 0009 drops the trigger and `one_liner`. Only 0009 is irreversible.

Expand, backfill and switch can each be rolled back without data loss. That's the point of splitting the change.

## 4. Large tables: operations that lock

SQLite takes a database-wide write lock for the whole migration transaction. Readers keep working (WAL), but no other write happens until it commits. Some statements take time proportional to the table's size. Before running, the runner looks for these (`migrate.py:48`) and counts the rows of each affected table (`migrate.py:250`):

| Statement | Why it's slow |
|---|---|
| `CREATE [UNIQUE] INDEX … ON t` | Reads and sorts every row |
| `ALTER TABLE t DROP COLUMN` | Rewrites the whole table |
| `UPDATE t SET …` with no `WHERE` | Rewrites every row |
| `DELETE FROM t` with no `WHERE` | Touches every row |
| `INSERT INTO … SELECT … FROM t` | Copies the whole table (the table-rebuild pattern) |

If an affected table has more than **100,000 rows** (`migrate.py:26`; change it with `--large-table-rows` or `PRIORART_LARGE_TABLE_ROWS`), the migration is refused unless the file contains a reason on its own line:

```sql
-- priorart: allow-blocking runs in the maintenance window; measured 4 s on a copy of production
```

A bare marker with no reason is refused.

**Safer patterns:**
- **Backfills:** batch by primary-key range, at most a few thousand rows per statement. Spread batches across several migrations, or run them from a script outside the migration.
- **New indexes on big tables:** build them in a migration run during low traffic, and say so in the allow-blocking reason with a measured duration from a production-sized copy.
- **Dropping a column:** don't. Leave it unused (contract can wait), or rebuild the table during a maintenance window with an allow-blocking reason.
- **Adding a column:** `ALTER TABLE … ADD COLUMN` is fast in SQLite (it doesn't rewrite rows), so it isn't guarded. `NOT NULL` needs a constant default.

**If the database moves to PostgreSQL**, the same rules map to:
- `CREATE INDEX CONCURRENTLY`, outside a transaction;
- `ALTER TABLE … ADD CONSTRAINT … NOT VALID` then `VALIDATE CONSTRAINT`;
- `SET lock_timeout` at the top of every migration;
- batched updates exactly as above.

## 5. Irreversible migrations

When the down path can't restore what the up path destroys, replace the `.down.sql` with `NNNN_name.irreversible.md`. The runner refuses the file unless both sections below exist and aren't empty:

```markdown
## Why
What this migration destroys, and why it can't be reconstructed (e.g. drops projects.one_liner after the
switch to projects.summary; values written only to one_liner after the switch are lost).

## Recovery
1. Stop the app (`PRIORART_ENV`, process manager, or deploy rollback).
2. Restore data/priorart.sqlite from the backup taken immediately before this migration
   (`sqlite3 data/priorart.sqlite ".backup data/pre-NNNN.sqlite"`).
3. Deploy the previous release, whose code expects the old schema.
4. Re-run `python scripts/db.py status` and confirm the version is NNNN-1.
```

Every irreversible migration also needs a backup step in its release checklist before it runs.

## 6. The CI check

`python scripts/db.py check` (`migrate.py:299`), run by `.github/workflows/ci.yml`:

1. creates an empty in-memory database and applies every migration;
2. rolls everything back to version 0, and fails if any table, index, view or trigger is left, or if any history row remains;
3. applies everything again, and fails if it errors or produces a different schema than step 1;
4. builds a second database straight from `src/priorart/schema.sql`, and fails on any difference ("drift"): an object missing from either side, or different SQL. Comments, whitespace and letter case are ignored (`migrate.py:280`).

**If the repo gains an irreversible migration,** step 2 can't pass through it. At that point, change the check to roll back to the newest irreversible version instead of 0. That's recorded under Follow-ups.

## 7. Local development

```bash
python scripts/db.py status          # what's applied and what's pending
python scripts/db.py up              # apply everything pending
python scripts/db.py up --to 3       # stop at version 0003
python scripts/db.py down            # undo the last migration
python scripts/db.py down --to 0     # undo everything
python scripts/db.py reset --yes     # down to 0 then up again (deletes all rows)
python scripts/db.py check           # what CI runs
```

`reset` needs `--yes` and refuses outright when `PRIORART_ENV=production` (`scripts/db.py:55`).

## Follow-ups (out of scope for this phase)

- **Index review.** Phase 03.3 audits the indexes 0001 created against the real access patterns and adjusts them in migration 0002.
- **CI with irreversible migrations.** Once the first irreversible migration exists, `check` should roll back only to the newest irreversible version (§6).
- **Automatic backup** before `up` when the pending set includes an irreversible migration.
- **The app doesn't use the database yet.** The pipeline and server still save JSON files (`src/priorart/pipeline.py:404–408`); moving them to the database is a later phase. That phase should also run `up` on server start, or refuse to start when migrations are pending.
- **The CI workflow only runs once the repo is on GitHub.** It hasn't run yet.

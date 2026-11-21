# Prior Art — Queries and Indexes

Phase 03 · Prompt 03.3 · Every query the application runs, the index that serves it, and the evidence.

## 0. Basis

- **The app doesn't query the database yet.** The pipeline and server still save JSON files (`src/priorart/pipeline.py:404–408`). So the access patterns come from:
  - what the server and page do today (`src/priorart/server.py:20–72`, `src/priorart/static/index.html`);
  - the screens and journeys in `docs/UX_ARCHITECTURE.md` and `docs/USER_JOURNEYS.md`;
  - the rows the finish trigger reads (`src/priorart/schema.sql`, `reports_finish_rules`).
- **Every query is defined once,** in `src/priorart/queries.py`. The future storage code uses that file, as do the plan capture (`scripts/explain_queries.py`) and the tests (`tests/test_query_plans.py`). A query that isn't in that file isn't allowed.
- **Measurements below** come from `python scripts/explain_queries.py` against generated volume after `ANALYZE`:
  - 1,000 owners and 4,000 reports;
  - 120,000 grid cells and 80,000 progress events;
  - SQLite 3.53.1.

## 1. Access pattern inventory

Frequencies come from the journeys in `docs/USER_JOURNEYS.md`. The table is ordered hottest first; the top five are the "hot" set whose plans and timings follow in §4.

| Query (`queries.py`) | Filter | Sort | Limit | Frequency | Served by |
|---|---|---|---|---|---|
| `owner_by_token` (`:32`) | `token_hash =` | — | 1 row (unique key) | every request | `UNIQUE (token_hash)` |
| `report_by_id` (`:35`) | `id =` | — | 1 row (primary key) | every report view and progress refresh | primary key |
| `recent_reports` (`:40`) | `owner_id =` | `created_at DESC` | 10 (`RECENT_PAGE`) | every Home visit and Recent menu open | `reports_owner_recent (owner_id, created_at DESC)`: filter, then sort, with no extra sort step |
| `report_overlaps` (`:54`) | `report_id =` | none (the page arranges cells) | 350 (50 projects × 7 features) | every report view | `overlaps_report_owner (report_id, owner_id)` |
| `report_events_after` (`:58`) | `report_id =`, `seq >` | `seq` | 200 (`EVENTS_PAGE`) | every progress refresh of a running report | `UNIQUE (report_id, seq)`: filter, range and sort in one |
| `report_features` (`:44`) | `report_id =` | `position` | 7 (schema cap) | every report view | `UNIQUE (report_id, position)` |
| `report_projects` (`:47`) | `report_id =` | `citation_number` | 50 (`MAX_PROJECTS`) | every report view | `UNIQUE (report_id, citation_number)`, then primary keys for the candidate and its query |
| `report_coverage` (`:62`) | `report_id =` | `position` | 7 | every report view | `UNIQUE (report_id, position)` and `overlaps_feature` |
| `report_check_totals` (`:68`) | `report_id =` | — | 1 row (aggregate) | every report view | `overlaps_report_owner` |
| `report_candidate_count` (`:76`) | `report_id =` | — | 1 row (aggregate) | every report view | `UNIQUE (report_id, url_key)`, read without touching the table |
| `report_pivots` (`:79`) | `report_id =` | `position` | 3 (schema cap) | every report view | `UNIQUE (report_id, position)` |
| `report_queries` (`:82`) | `report_id =` | `position` | 14 (schema cap) | progress refreshes; failed or low-evidence views | `UNIQUE (report_id, position)` |
| `pivots_of_report` (`:86`) | `pivot_of_report_id =` | `created_at DESC` | 20 | report views that have pivots checked | `reports_pivot_of` (partial). The sort runs over a handful of rows (see §3). |
| `candidate_by_url_key` (`:90`) | `report_id =`, `url_key =` | — | 1 row (unique key) | each search result while a check runs | `UNIQUE (report_id, url_key)`, read without touching the table |

**Unbounded reads.** Every list query ends in `LIMIT ?`, enforced by `test_every_list_query_has_a_limit`. Lists the schema already caps still carry the cap, so a schema change can't make them unbounded. Three reads would grow without limit if written naively, and each is bounded here:
- **Grid cells:** the database doesn't cap projects per report; the pipeline's 8 is only a default. Capped at 350 cells.
- **Progress events:** nothing caps a report's event count. Paged 200 at a time from the last seen `seq`.
- **Recent reports:** the heavy seeded owner has 121. Paged 10 at a time.

**Rejected claims and gaps** are computed from the same `report_overlaps` and `report_coverage` rows, with no extra query.

## 2. Foreign-key lookups

SQLite looks up child rows on every cascading delete or update. Without an index, deleting one report would scan every child table in full. Each lookup's equivalent query is in `queries.py` `FK_LOOKUPS` (`:97`), and `test_every_foreign_key_lookup_is_indexed` checks each one.

| Child key | Served by |
|---|---|
| `reports.owner_id` | `reports_owner_recent`, read without touching the table |
| `reports.pivot_of_report_id` | `reports_pivot_of`, read without touching the table |
| `features (report_id, owner_id)` | `UNIQUE (report_id, position)` |
| `search_queries (report_id, owner_id)` | `UNIQUE (report_id, position)` |
| `candidates (report_id, owner_id)` | `UNIQUE (report_id, url_key)` |
| `candidates (found_by_query_id, report_id)` | `candidates_query`, read without touching the table |
| `projects (report_id, owner_id)` | `UNIQUE (report_id, citation_number)` |
| `projects (candidate_id, report_id)` | `UNIQUE (candidate_id)` |
| `overlaps (report_id, owner_id)` | `overlaps_report_owner`, read without touching the table |
| `overlaps (project_id, report_id)` | `UNIQUE (project_id, feature_id)` |
| `overlaps (feature_id, report_id)` | `overlaps_feature`, read without touching the table |
| `pivots (report_id, owner_id)` | `UNIQUE (report_id, position)` |
| `report_events (report_id, owner_id)` | `UNIQUE (report_id, seq)` |

The finish trigger's 8 lookups (`queries.py` `TRIGGER_LOOKUPS`, `:115`) are checked the same way by `test_finish_trigger_lookups_are_indexed`.

## 3. The index set

**Unique indexes** come from the UNIQUE constraints in `docs/DATA_MODEL.md` §6, so each one encodes a rule, and none is redundant. The `UNIQUE (id, report_id)` and `UNIQUE (id, owner_id)` indexes exist because a composite foreign key needs a unique target. They serve no reads and cost one extra insert into an index per row.

**Named indexes after migration 0002.** Write cost was measured by inserting the table's rows into a bare copy with and without the index (best of 3), and size comes from `dbstat`:

| Index | Serves | Extra insert time | Size at volume |
|---|---|---|---|
| `reports_owner_recent (owner_id, created_at DESC)` | `recent_reports`; `reports.owner_id` FK | 1.2 ms per 4,000 rows (3.0 ms / 10k) | 160 KB |
| `reports_pivot_of (pivot_of_report_id) WHERE pivot_of_report_id IS NOT NULL` | `pivots_of_report`; pivot FK. Partial: only reports that are pivots are indexed. | ~0 ms (0.09 ms / 10k) | 4 KB |
| `candidates_query (found_by_query_id, report_id)` | candidates → query FK | 3.4 ms per 24,000 rows (1.4 ms / 10k) | 564 KB |
| `overlaps_feature (feature_id, report_id)` | `report_coverage`; overlaps → feature FK | 40.3 ms per 120,000 rows (3.4 ms / 10k) | 2,932 KB |
| `overlaps_report_owner (report_id, owner_id)` | `report_overlaps`, `report_check_totals`; overlaps → report FK | 59.2 ms per 120,000 rows (4.9 ms / 10k) | 2,972 KB |

**Removed by 0002 (`migrations/0002_index_review.up.sql`).** Each was a plain index whose leading column already led a unique index on the same table, so the unique index served the same lookups. Each cost an extra insert into an index per row and gave nothing back:

| Removed | Served instead by |
|---|---|
| `features_report_owner` | `UNIQUE (report_id, position)` |
| `search_queries_report_owner` | `UNIQUE (report_id, position)` |
| `candidates_report_owner` | `UNIQUE (report_id, url_key)` |
| `projects_report_owner` | `UNIQUE (report_id, citation_number)` |
| `pivots_report_owner` | `UNIQUE (report_id, position)` |
| `report_events_report_owner` | `UNIQUE (report_id, seq)` |
| `projects_candidate` | `UNIQUE (candidate_id)` |
| `overlaps_project` | `UNIQUE (project_id, feature_id)` |

`test_no_overlapping_indexes` keeps this from coming back. `test_overlap_check_catches_the_0001_index_set` proves the check finds exactly these eight when run on the 0001 schema.

**Deliberately not added:**
- An index that removes `pivots_of_report`'s small sort, `(pivot_of_report_id, created_at)`. One report has a handful of pivots checked, so sorting them in memory is cheaper than maintaining another index.
- Covering indexes for `recent_reports`. It reads at most 10 rows from the table after the index lookup (10 µs median).

## 4. Query plans for the 5 hottest queries

`EXPLAIN QUERY PLAN` and median time over 200 runs against real ids, at the volume in §0:

```
owner_by_token  median 2 µs
    SEARCH owners USING INDEX sqlite_autoindex_owners_1 (token_hash=?)
report_by_id  median 4 µs
    SEARCH reports USING INDEX sqlite_autoindex_reports_1 (id=?)
recent_reports  median 10 µs
    SEARCH reports USING INDEX reports_owner_recent (owner_id=?)
report_overlaps  median 30 µs
    SEARCH overlaps USING INDEX overlaps_report_owner (report_id=?)
report_events_after  median 13 µs
    SEARCH report_events USING INDEX sqlite_autoindex_report_events_1 (report_id=? AND seq>?)
```

- **No full-table scans:** every plan line is a `SEARCH` with a named index.
- **No extra sort steps:** `recent_reports` and `report_events_after` get their order straight from the index.
- **Full output:** all 14 queries, all foreign-key lookups and the write-cost table come from `python scripts/explain_queries.py`. It exits with an error if any plan scans a table.

## 5. The bug the plans found: finishing a report got slower as data grew

Generating the 4,000 reports took **195.8 s** on the 0002 schema. That was far slower than linear: 800 reports had taken 7.8 s.

**The cause:** the finish trigger checks the stored novelty against `feature_coverage WHERE report_id = ?`. That view grouped by `f.id` only, so SQLite couldn't push the report filter into it, and it scanned every feature in the database on every finish:

```
CO-ROUTINE feature_coverage
SCAN f USING INDEX sqlite_autoindex_features_2      ← whole table
SEARCH o USING INDEX overlaps_feature (feature_id=?) LEFT-JOIN
```

**The fix:** migration `0003_coverage_view_pushdown` groups the view by `(report_id, position)`, which is unique per report, and joins on `report_id` as well:

```
CO-ROUTINE feature_coverage
SEARCH f USING COVERING INDEX sqlite_autoindex_features_1 (report_id=?)
SEARCH o USING INDEX overlaps_feature (feature_id=? AND report_id=?) LEFT-JOIN
```

The same generation then took **8.8 s, 22× faster**. `test_trigger_check_catches_the_pre_0003_view` proves the test catches the old view. The `SCAN feature_coverage` line that remains only walks the view's own result rows: at most 7, one per feature.

## 6. Added by 0004 (lifecycle, `docs/DATA_LIFECYCLE.md`)

- **Soft delete in existing reads:** `report_by_id`, `recent_reports` and `pivots_of_report` now also filter `deleted_at IS NULL`. Their plans are unchanged: the same index lookups, plus a check on the few rows they return.
- **New queries,** all bounded and all served by an index (measured at the §0 volume, schema 0004):

| Query | Filter / sort / limit | Frequency | Plan |
|---|---|---|---|
| `recently_deleted` | `owner_id =`, `deleted_at IS NOT NULL` / `deleted_at DESC` / 10 | rare (Recently deleted view) | `SEARCH reports USING INDEX reports_purge_due (deleted_at>?)`: walks only deleted reports. Fine while few reports are deleted at once; switch to the owner index if that ever stops being true. |
| `purge_due` | `deleted_at < cutoff` / `deleted_at` / 200 | daily purge | `SEARCH reports USING INDEX reports_purge_due (deleted_at>? AND deleted_at<?)` |
| `owner_reports_page` | `owner_id =`, `(created_at, id) >` / `created_at, id` / 100 | rare (export) | `SEARCH reports USING INDEX reports_owner_recent (owner_id=? AND created_at>?)`, plus a small sort for ties within one page |
| `owner_audit_page` | `owner_ref =`, `id >` / `id` / 500 | rare (export) | `SEARCH audit_events USING INDEX audit_events_owner (owner_ref=? AND rowid>?)`: no sort, because SQLite stores the rowid in every index |

- **New indexes:**

| Index | Serves | Write cost |
|---|---|---|
| `reports_purge_due (deleted_at) WHERE deleted_at IS NOT NULL` | `purge_due`, `recently_deleted` | Partial: only soft-deleted reports are in it, so live inserts cost nothing (0.11 ms / 10k measured; 4 KB) |
| `audit_events_owner (owner_ref)` | `owner_audit_page` | One insert into this index per audit entry. Audit entries are rare (deletes, restores, exports), so the measured cost on the empty volume table is negligible. |

- **Totals at schema 0004:** every query and foreign-key lookup still uses an index; the plan script reports none that scans a whole table.

## Follow-ups (out of scope for this phase)

- **Storage layer.** Use these queries when moving storage from JSON files to the database (later phase), and add no query outside `queries.py`.
- **Paging.** The page should request events in pages of `EVENTS_PAGE` and Recent reports in pages of `RECENT_PAGE` (09.1 / 09.3 designs). Today the page has neither.
- **Re-measuring.** Re-run `scripts/explain_queries.py` whenever a migration touches indexes or views. Consider adding it to CI once the repo is on GitHub; at default volume it takes about 10 s.

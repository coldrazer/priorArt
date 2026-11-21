"""Every read the application runs against the database, in one place (docs/QUERY_PLAN.md maps each to its index).

Rules:
- Every list query has a LIMIT, bound as its last parameter. Lists that the schema already caps (≤ 7 features,
  ≤ 14 searches, ≤ 3 pivots) still carry the cap as their limit, so a schema change can't make them unbounded.
- Report reads filter by report id first: every per-report index starts with report_id.
- No SELECT *: columns are listed so a new column can't silently widen a hot query.
- Soft delete (docs/DATA_LIFECYCLE.md): every query that reads `reports` filters `deleted_at IS NULL`, except
  the lifecycle queries marked includes_deleted. Per-report child queries run only after report_by_id has
  returned the report, so a deleted report's parts are never read through the app.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Query:
    name: str
    sql: str
    purpose: str
    frequency: str  # how often it runs, from docs/USER_JOURNEYS.md
    default_limit: int | None = None  # None: returns at most one row by construction
    includes_deleted: bool = False  # True only for the lifecycle queries that must see soft-deleted reports


# Limits for lists the schema doesn't cap. MAX_PROJECTS is far above the pipeline's default of 8
# (config.py max_candidates) so a deployment that raises it still reads complete grids.
MAX_PROJECTS = 50
MAX_FEATURES = 7
MAX_GRID_CELLS = MAX_PROJECTS * MAX_FEATURES
RECENT_PAGE = 10
EVENTS_PAGE = 200

QUERIES: dict[str, Query] = {
    q.name: q
    for q in [
        Query(
            "owner_by_token",
            "SELECT id, created_at FROM owners WHERE token_hash = ?",
            "Identify the browser's owner from its cookie token hash",
            "every request",
        ),
        Query(
            "report_by_id",
            "SELECT id, owner_id, pivot_of_report_id, idea, summary, target_user, status, status_message, novelty, "
            "verdict, writer_model, verifier_model, simulate, started_at, finished_at, created_at "
            "FROM reports WHERE id = ? AND deleted_at IS NULL",
            "Open a report (/r/{id}), running or finished",
            "every report view and every progress refresh",
        ),
        Query(
            "recent_reports",
            "SELECT id, summary, status, novelty, created_at FROM reports "
            "WHERE owner_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
            "Recent reports on Home and in the header's Recent menu",
            "every Home visit and menu open",
            RECENT_PAGE,
        ),
        Query(
            "report_features",
            "SELECT id, position, name, description FROM features WHERE report_id = ? ORDER BY position LIMIT ?",
            "Grid columns, in order",
            "every report view",
            MAX_FEATURES,
        ),
        Query(
            "report_projects",
            "SELECT p.id, p.citation_number, p.name, p.one_liner, p.kind, p.page_source, p.injection_excerpt, "
            "c.url, q.source_type FROM projects p "
            "JOIN candidates c ON c.id = p.candidate_id "
            "JOIN search_queries q ON q.id = c.found_by_query_id "
            "WHERE p.report_id = ? ORDER BY p.citation_number LIMIT ?",
            "Grid rows and the numbered Sources list",
            "every report view",
            MAX_PROJECTS,
        ),
        Query(
            "report_overlaps",
            "SELECT project_id, feature_id, claimed_score, final_score, quote, check_status, check_note "
            "FROM overlaps WHERE report_id = ? LIMIT ?",
            "Grid cells, evidence drawer and Rejected claims",
            "every report view",
            MAX_GRID_CELLS,
        ),
        Query(
            "report_events_after",
            "SELECT seq, kind, payload FROM report_events WHERE report_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            "Replay or continue a check's progress log (resume after leaving)",
            "every progress refresh of a running report",
            EVENTS_PAGE,
        ),
        Query(
            "report_coverage",
            "SELECT f.id, f.position, COALESCE(MAX(o.final_score), 0) AS best_score, "
            "COUNT(CASE WHEN o.final_score >= 1 THEN 1 END) AS projects_covering "
            "FROM features f LEFT JOIN overlaps o ON o.feature_id = f.id AND o.report_id = f.report_id "
            "WHERE f.report_id = ? GROUP BY f.position ORDER BY f.position LIMIT ?",
            "Best-coverage row and Open (gap) columns",
            "every report view",
            MAX_FEATURES,
        ),
        Query(
            "report_check_totals",
            "SELECT COUNT(CASE WHEN claimed_score > 0 THEN 1 END), "
            "COUNT(CASE WHEN check_status = 'cut_quote' THEN 1 END), "
            "COUNT(CASE WHEN check_status = 'cut_verifier' THEN 1 END), "
            "COUNT(CASE WHEN check_status = 'downgraded' THEN 1 END), "
            "COUNT(CASE WHEN check_status = 'unverified' THEN 1 END) "
            "FROM overlaps WHERE report_id = ?",
            "The check counts in the report summary",
            "every report view",
        ),
        Query(
            "report_candidate_count",
            "SELECT COUNT(*) FROM candidates WHERE report_id = ?",
            "'N closest projects from M results'",
            "every report view",
        ),
        Query(
            "report_pivots",
            "SELECT position, title, pitch, why_open FROM pivots WHERE report_id = ? ORDER BY position LIMIT ?",
            "Gaps & pivots section",
            "every report view",
            3,
        ),
        Query(
            "report_queries",
            "SELECT position, source_type, query_text, outcome, result_count, error_message FROM search_queries "
            "WHERE report_id = ? ORDER BY position LIMIT ?",
            "Search progress while running; failed-source notes",
            "progress refreshes; failed or low-evidence views",
            14,
        ),
        Query(
            "pivots_of_report",
            "SELECT id, summary, status, novelty FROM reports "
            "WHERE pivot_of_report_id = ? AND owner_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            "'Checked as a pivot' links from a report, scoped to the viewing owner",
            "report views that have pivots checked",
            20,
        ),
        Query(
            "candidate_by_url_key",
            "SELECT id FROM candidates WHERE report_id = ? AND url_key = ?",
            "Write path: find an existing candidate before inserting (dedupe)",
            "each search result while checking",
        ),
        # ---- lifecycle (docs/DATA_LIFECYCLE.md) ----
        Query(
            "recently_deleted",
            "SELECT id, summary, deleted_at FROM reports WHERE owner_id = ? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT ?",
            "An owner's deleted reports that can still be restored",
            "rare: owner opens Recently deleted",
            RECENT_PAGE,
            includes_deleted=True,
        ),
        Query(
            "purge_due",
            "SELECT id FROM reports WHERE deleted_at IS NOT NULL AND deleted_at < ? ORDER BY deleted_at LIMIT ?",
            "Soft-deleted reports past the 30-day window, oldest first",
            "each purge run (daily)",
            200,
            includes_deleted=True,
        ),
        Query(
            "owner_reports_page",
            "SELECT id, created_at FROM reports WHERE owner_id = ? AND (created_at, id) > (?, ?) "
            "ORDER BY created_at, id LIMIT ?",
            "Export: all of an owner's reports, deleted ones included, a page at a time",
            "rare: data export",
            100,
            includes_deleted=True,
        ),
        Query(
            "owner_audit_page",
            "SELECT id, actor, action, report_ref, before, after, created_at FROM audit_events "
            "WHERE owner_ref = ? AND id > ? ORDER BY id LIMIT ?",
            "Export: the audit entries about an owner",
            "rare: data export",
            500,
        ),
    ]
}

# Lookups SQLite itself runs to enforce foreign keys (ON DELETE / ON UPDATE CASCADE). Each must be indexed or a
# delete of one parent row scans the whole child table. SQL here is the equivalent lookup, for EXPLAIN.
FK_LOOKUPS: dict[str, str] = {
    "reports.owner_id": "SELECT 1 FROM reports WHERE owner_id = ?",
    "reports.pivot_of_report_id": "SELECT 1 FROM reports WHERE pivot_of_report_id = ?",
    "features.(report_id, owner_id)": "SELECT 1 FROM features WHERE report_id = ? AND owner_id = ?",
    "search_queries.(report_id, owner_id)": "SELECT 1 FROM search_queries WHERE report_id = ? AND owner_id = ?",
    "candidates.(report_id, owner_id)": "SELECT 1 FROM candidates WHERE report_id = ? AND owner_id = ?",
    "candidates.(found_by_query_id, report_id)": "SELECT 1 FROM candidates WHERE found_by_query_id = ? AND report_id = ?",
    "projects.(report_id, owner_id)": "SELECT 1 FROM projects WHERE report_id = ? AND owner_id = ?",
    "projects.(candidate_id, report_id)": "SELECT 1 FROM projects WHERE candidate_id = ? AND report_id = ?",
    "overlaps.(report_id, owner_id)": "SELECT 1 FROM overlaps WHERE report_id = ? AND owner_id = ?",
    "overlaps.(project_id, report_id)": "SELECT 1 FROM overlaps WHERE project_id = ? AND report_id = ?",
    "overlaps.(feature_id, report_id)": "SELECT 1 FROM overlaps WHERE feature_id = ? AND report_id = ?",
    "pivots.(report_id, owner_id)": "SELECT 1 FROM pivots WHERE report_id = ? AND owner_id = ?",
    "report_events.(report_id, owner_id)": "SELECT 1 FROM report_events WHERE report_id = ? AND owner_id = ?",
}

# Lookups the finish trigger (reports_finish_rules) runs once per finished report. They must stay indexed, or
# finishing a report gets slower as the database grows (the bug migration 0003 fixed).
TRIGGER_LOOKUPS: dict[str, str] = {
    "coverage sum": "SELECT SUM(best_score) FROM feature_coverage WHERE report_id = ?",
    "feature count": "SELECT COUNT(*) FROM features WHERE report_id = ?",
    "project count": "SELECT COUNT(*) FROM projects WHERE report_id = ?",
    "max citation": "SELECT COALESCE(MAX(citation_number), 0) FROM projects WHERE report_id = ?",
    "uncited project": "SELECT 1 FROM projects WHERE report_id = ? AND citation_number IS NULL",
    "grid cell count": "SELECT COUNT(*) FROM overlaps WHERE report_id = ?",
    "any live overlap": "SELECT 1 FROM overlaps WHERE report_id = ? AND final_score > 0",
    "any pivot": "SELECT 1 FROM pivots WHERE report_id = ?",
}

HOT = ["owner_by_token", "report_by_id", "recent_reports", "report_overlaps", "report_events_after"]


def params_for(q: Query, *args: Any) -> tuple[Any, ...]:
    """Bind a query's arguments plus its LIMIT (if it has one)."""
    return (*args, q.default_limit) if q.default_limit is not None else tuple(args)

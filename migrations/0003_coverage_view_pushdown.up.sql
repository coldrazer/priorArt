-- 0003_coverage_view_pushdown (up): make "feature_coverage WHERE report_id = ?" use an index.
-- The old view grouped by f.id only, so SQLite couldn't push a report_id filter into it and scanned every
-- feature in the database. The finish trigger (reports_finish_rules) runs that filter on every finished report,
-- so finishing got slower as the database grew (docs/QUERY_PLAN.md §5).
-- Grouping by (report_id, position), which is unique per report, lets the filter use UNIQUE (report_id, position).
DROP VIEW report_gaps;
DROP VIEW feature_coverage;
CREATE VIEW feature_coverage AS
SELECT f.report_id, f.id AS feature_id, f.position,
       COALESCE(MAX(o.final_score), 0) AS best_score,
       COUNT(CASE WHEN o.final_score >= 1 THEN 1 END) AS projects_covering
FROM features f LEFT JOIN overlaps o ON o.feature_id = f.id AND o.report_id = f.report_id
GROUP BY f.report_id, f.position;
CREATE VIEW report_gaps AS
SELECT report_id, feature_id, position FROM feature_coverage WHERE best_score = 0;

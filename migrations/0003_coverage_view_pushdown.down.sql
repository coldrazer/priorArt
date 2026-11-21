-- 0003_coverage_view_pushdown (down): restore the views exactly as 0001_initial defined them.
DROP VIEW report_gaps;
DROP VIEW feature_coverage;
CREATE VIEW feature_coverage AS
SELECT f.report_id, f.id AS feature_id, f.position,
       COALESCE(MAX(o.final_score), 0) AS best_score,
       COUNT(CASE WHEN o.final_score >= 1 THEN 1 END) AS projects_covering
FROM features f LEFT JOIN overlaps o ON o.feature_id = f.id
GROUP BY f.id;
CREATE VIEW report_gaps AS
SELECT report_id, feature_id, position FROM feature_coverage WHERE best_score = 0;

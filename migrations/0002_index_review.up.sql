-- 0002_index_review (up): remove indexes that duplicate a UNIQUE constraint's index (docs/QUERY_PLAN.md §3).
-- Each lookup they served is served by the unique index named on its line, which starts with the same column(s).
DROP INDEX features_report_owner;        -- → UNIQUE (report_id, position)
DROP INDEX search_queries_report_owner;  -- → UNIQUE (report_id, position)
DROP INDEX candidates_report_owner;      -- → UNIQUE (report_id, url_key)
DROP INDEX projects_report_owner;        -- → UNIQUE (report_id, citation_number)
DROP INDEX pivots_report_owner;          -- → UNIQUE (report_id, position)
DROP INDEX report_events_report_owner;   -- → UNIQUE (report_id, seq)
DROP INDEX projects_candidate;           -- → UNIQUE (candidate_id)
DROP INDEX overlaps_project;             -- → UNIQUE (project_id, feature_id)

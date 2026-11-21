-- 0002_index_review (down): recreate the eight indexes exactly as 0001_initial defined them.
CREATE INDEX features_report_owner ON features (report_id, owner_id);
CREATE INDEX search_queries_report_owner ON search_queries (report_id, owner_id);
CREATE INDEX candidates_report_owner ON candidates (report_id, owner_id);
CREATE INDEX projects_report_owner ON projects (report_id, owner_id);
CREATE INDEX pivots_report_owner ON pivots (report_id, owner_id);
CREATE INDEX report_events_report_owner ON report_events (report_id, owner_id);
CREATE INDEX projects_candidate ON projects (candidate_id, report_id);
CREATE INDEX overlaps_project ON overlaps (project_id, report_id);

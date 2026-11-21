-- 0001_initial (down): removes everything 0001_initial.up.sql created.
-- Views first, then tables children-before-parents so no foreign key is ever left dangling.
-- Triggers and indexes are dropped with their tables.
DROP VIEW report_candidate_counts;
DROP VIEW report_check_totals;
DROP VIEW report_gaps;
DROP VIEW feature_coverage;
DROP TABLE report_events;
DROP TABLE pivots;
DROP TABLE overlaps;
DROP TABLE projects;
DROP TABLE candidates;
DROP TABLE search_queries;
DROP TABLE features;
DROP TABLE reports;
DROP TABLE owners;

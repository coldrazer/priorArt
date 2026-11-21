-- 0005_production_runtime (down): reverses 0005 cleanly.
-- DROP COLUMN rebuilds the table, so on tables above the large-table limit the lock guard requires a window.
DROP INDEX usage_events_check;
DROP INDEX usage_events_ts;
DROP TABLE usage_events;
DROP TABLE rate_buckets;
DROP INDEX reports_pivot_of;
CREATE INDEX reports_pivot_of ON reports (pivot_of_report_id) WHERE pivot_of_report_id IS NOT NULL;
DROP INDEX reports_running_heartbeat;
DROP INDEX reports_idempotency;
ALTER TABLE reports DROP COLUMN heartbeat_at;
ALTER TABLE reports DROP COLUMN client_request_id;
ALTER TABLE owners DROP COLUMN last_seen_at;

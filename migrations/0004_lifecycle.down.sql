-- 0004_lifecycle (down): remove soft delete and the audit trail.
-- Reports their owners had already deleted are removed for good here rather than becoming visible again: the
-- owner asked for them to go. The audit trail is dropped with its table; take a backup first if it must be kept
-- (scripts/backup.py backup). DROP COLUMN rebuilds the reports table, so on a table over the large-table limit
-- the runner refuses this file until it's run in a maintenance window (docs/MIGRATIONS.md §4).
DROP TRIGGER report_events_append_only;
CREATE TRIGGER report_events_append_only BEFORE UPDATE ON report_events
BEGIN SELECT RAISE(ABORT, 'report events are append-only'); END;
DROP TRIGGER audit_context_touch;
DROP TRIGGER audit_owner_deleted;
DROP TRIGGER audit_report_reassigned;
DROP TRIGGER audit_report_purged;
DROP TRIGGER audit_report_restored;
DROP TRIGGER audit_report_deleted;
DROP TRIGGER reports_running_not_deletable;
DROP TRIGGER audit_events_no_delete;
DROP TRIGGER audit_events_append_only;
DROP INDEX reports_purge_due;
DROP INDEX audit_events_owner;
DROP TABLE audit_events;
DROP TABLE audit_context;
DELETE FROM reports WHERE deleted_at IS NOT NULL;
ALTER TABLE reports DROP COLUMN deleted_at;

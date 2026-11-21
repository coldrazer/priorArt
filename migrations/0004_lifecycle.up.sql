-- 0004_lifecycle (up): soft delete for reports, an append-only audit trail, and the purge index.
-- Policy and rationale: docs/DATA_LIFECYCLE.md.

-- Soft delete. A deleted report is hidden at once and restorable for 30 days; purge then removes it for good.
-- ADD COLUMN doesn't rewrite the table, so it's safe at any size.
ALTER TABLE reports ADD COLUMN deleted_at TEXT CHECK (deleted_at IS NULL OR (deleted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z' AND deleted_at >= created_at));

-- Who is acting, for the audit triggers below: one row, set by priorart.lifecycle inside each transaction and
-- reset before commit. Anything else (a hand-run SQL statement) is recorded as 'sql'.
CREATE TABLE audit_context (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  actor       TEXT NOT NULL DEFAULT 'sql' CHECK (actor IN ('sql', 'owner', 'operator', 'seed', 'purge', 'system')),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z')
) STRICT;
INSERT INTO audit_context (id) VALUES (1);

-- The audit trail: who, what, when, before and after. Holds no idea text or other content, only ids, statuses
-- and timestamps, so it can outlive the rows it describes. owner_ref and report_ref are deliberately NOT foreign
-- keys: a deleted owner's or report's audit entries must survive the deletion.
CREATE TABLE audit_events (
  id          INTEGER PRIMARY KEY,
  actor       TEXT NOT NULL CHECK (actor IN ('sql', 'owner', 'operator', 'seed', 'purge', 'system')),
  action      TEXT NOT NULL CHECK (action IN ('report_deleted', 'report_restored', 'report_purged', 'owner_deleted',
                                              'owner_reassigned', 'owner_exported')),
  owner_ref   INTEGER,
  report_ref  TEXT,
  before      TEXT CHECK (before IS NULL OR (json_valid(before) AND json_type(before) = 'object')),
  after       TEXT CHECK (after IS NULL OR (json_valid(after) AND json_type(after) = 'object')),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  CHECK (owner_ref IS NOT NULL OR report_ref IS NOT NULL)
) STRICT;

CREATE INDEX audit_events_owner ON audit_events (owner_ref);  -- SQLite appends the rowid, so this also orders by id
CREATE INDEX reports_purge_due ON reports (deleted_at) WHERE deleted_at IS NOT NULL;

-- 0001's report_events_append_only blocked every UPDATE, including the ON UPDATE CASCADE of owner_id, so a report
-- with a progress log could never change owner. The cascade also re-sets report_id to its unchanged value, so
-- the guard compares values: it rejects only a real change to an event's content.
DROP TRIGGER report_events_append_only;
CREATE TRIGGER report_events_append_only BEFORE UPDATE ON report_events
WHEN NEW.report_id IS NOT OLD.report_id OR NEW.seq IS NOT OLD.seq OR NEW.kind IS NOT OLD.kind
  OR NEW.payload IS NOT OLD.payload OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'report events are append-only'); END;

CREATE TRIGGER audit_events_append_only BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;

CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events cannot be deleted'); END;

CREATE TRIGGER reports_running_not_deletable BEFORE UPDATE OF deleted_at ON reports
WHEN NEW.deleted_at IS NOT NULL AND OLD.status = 'running'
BEGIN SELECT RAISE(ABORT, 'a running check cannot be deleted; wait for it to finish'); END;

CREATE TRIGGER audit_report_deleted AFTER UPDATE OF deleted_at ON reports
WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
BEGIN
  INSERT INTO audit_events (actor, action, owner_ref, report_ref, before, after)
  VALUES ((SELECT actor FROM audit_context WHERE id = 1), 'report_deleted', NEW.owner_id, NEW.id,
          json_object('deleted_at', NULL), json_object('deleted_at', NEW.deleted_at));
END;

CREATE TRIGGER audit_report_restored AFTER UPDATE OF deleted_at ON reports
WHEN OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL
BEGIN
  INSERT INTO audit_events (actor, action, owner_ref, report_ref, before, after)
  VALUES ((SELECT actor FROM audit_context WHERE id = 1), 'report_restored', NEW.owner_id, NEW.id,
          json_object('deleted_at', OLD.deleted_at), json_object('deleted_at', NULL));
END;

CREATE TRIGGER audit_report_purged AFTER DELETE ON reports
BEGIN
  INSERT INTO audit_events (actor, action, owner_ref, report_ref, before, after)
  VALUES ((SELECT actor FROM audit_context WHERE id = 1), 'report_purged', OLD.owner_id, OLD.id,
          json_object('status', OLD.status, 'created_at', OLD.created_at, 'deleted_at', OLD.deleted_at), NULL);
END;

CREATE TRIGGER audit_report_reassigned AFTER UPDATE OF owner_id ON reports
WHEN NEW.owner_id IS NOT OLD.owner_id
BEGIN
  INSERT INTO audit_events (actor, action, owner_ref, report_ref, before, after)
  VALUES ((SELECT actor FROM audit_context WHERE id = 1), 'owner_reassigned', NEW.owner_id, NEW.id,
          json_object('owner_id', OLD.owner_id), json_object('owner_id', NEW.owner_id));
END;

CREATE TRIGGER audit_owner_deleted BEFORE DELETE ON owners
BEGIN
  INSERT INTO audit_events (actor, action, owner_ref, before, after)
  VALUES ((SELECT actor FROM audit_context WHERE id = 1), 'owner_deleted', OLD.id,
          json_object('reports', (SELECT COUNT(*) FROM reports WHERE owner_id = OLD.id), 'created_at', OLD.created_at),
          NULL);
END;

CREATE TRIGGER audit_context_touch AFTER UPDATE ON audit_context
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE audit_context SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

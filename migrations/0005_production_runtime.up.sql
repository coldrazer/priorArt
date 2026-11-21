-- 0005_production_runtime (up): idempotency keys, running-check heartbeats, owner last_seen_at,
-- owner-scoped pivot index, SQLite token buckets, and provider usage ledger.

ALTER TABLE owners ADD COLUMN last_seen_at TEXT CHECK (last_seen_at IS NULL OR last_seen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z');

ALTER TABLE reports ADD COLUMN client_request_id TEXT CHECK (client_request_id IS NULL OR length(trim(client_request_id)) BETWEEN 1 AND 128);
ALTER TABLE reports ADD COLUMN heartbeat_at TEXT CHECK (heartbeat_at IS NULL OR heartbeat_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z');

CREATE UNIQUE INDEX reports_idempotency ON reports (owner_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE INDEX reports_running_heartbeat ON reports (heartbeat_at) WHERE status = 'running';

-- Scope pivot-of lookups by owner so another owner's pivot never leaks onto a report.
DROP INDEX reports_pivot_of;
CREATE INDEX reports_pivot_of ON reports (pivot_of_report_id, owner_id) WHERE pivot_of_report_id IS NOT NULL;

CREATE TABLE rate_buckets (
  key         TEXT PRIMARY KEY CHECK (length(trim(key)) > 0),
  tokens      REAL NOT NULL CHECK (tokens >= 0),
  updated_at  REAL NOT NULL CHECK (updated_at >= 0)
) STRICT;

CREATE TABLE usage_events (
  id                 INTEGER PRIMARY KEY,
  ts                 TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  check_id           TEXT,
  owner_ref          TEXT,
  ip_ref             TEXT,
  provider           TEXT NOT NULL CHECK (provider IN ('nebius', 'tavily')),
  operation          TEXT NOT NULL CHECK (length(trim(operation)) > 0),
  model              TEXT NOT NULL DEFAULT '',
  prompt_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
  completion_tokens  INTEGER NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
  credits            REAL NOT NULL DEFAULT 0 CHECK (credits >= 0),
  cached             INTEGER NOT NULL DEFAULT 0 CHECK (cached IN (0, 1)),
  attempt            INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1)
) STRICT;

CREATE INDEX usage_events_ts ON usage_events (ts);
CREATE INDEX usage_events_check ON usage_events (check_id) WHERE check_id IS NOT NULL;

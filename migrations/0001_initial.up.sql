-- 0001_initial (up): the v1 schema from docs/DATA_MODEL.md. Must stay identical in effect to src/priorart/schema.sql.

CREATE TABLE owners (
  id           INTEGER PRIMARY KEY,
  -- SHA-256 (hex) of the random token kept in the owner's browser cookie; the token itself is never stored.
  token_hash   TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  CHECK (updated_at >= created_at)
) STRICT;

CREATE TABLE reports (
  id                  TEXT PRIMARY KEY CHECK (length(id) = 10 AND id NOT GLOB '*[^0-9A-Za-z]*'),
  owner_id            INTEGER NOT NULL REFERENCES owners (id) ON DELETE CASCADE,
  pivot_of_report_id  TEXT REFERENCES reports (id) ON DELETE SET NULL,
  idea                TEXT NOT NULL CHECK (length(trim(idea)) BETWEEN 10 AND 4000),
  summary             TEXT CHECK (summary IS NULL OR length(trim(summary)) BETWEEN 1 AND 500),
  target_user         TEXT NOT NULL DEFAULT '',
  status              TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'ok', 'no_close_match', 'insufficient_evidence', 'failed')),
  status_message      TEXT NOT NULL DEFAULT '',
  -- Deliberate denormalization: novelty is derivable from overlaps (view feature_coverage). It is stored because it is
  -- the number the user was shown when the check finished; trigger reports_finish_rules keeps it within 0.5 of the
  -- value recomputed from checked coverage.
  novelty             INTEGER CHECK (novelty BETWEEN 0 AND 100),
  verdict             TEXT NOT NULL DEFAULT '',
  writer_model        TEXT NOT NULL CHECK (length(trim(writer_model)) > 0),
  verifier_model      TEXT NOT NULL CHECK (length(trim(verifier_model)) > 0),
  simulate            TEXT CHECK (simulate IN ('search_down', 'extract_down')),
  started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  finished_at         TEXT CHECK (finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  CHECK ((status = 'running') = (finished_at IS NULL)),
  CHECK (finished_at IS NULL OR finished_at >= started_at),
  CHECK (CASE WHEN status IN ('ok', 'no_close_match') THEN novelty IS NOT NULL ELSE novelty IS NULL END),
  CHECK (status NOT IN ('ok', 'no_close_match') OR summary IS NOT NULL),
  CHECK (status IN ('running', 'ok') OR length(trim(status_message)) > 0),
  CHECK (pivot_of_report_id IS NULL OR pivot_of_report_id <> id),
  CHECK (updated_at >= created_at),
  UNIQUE (id, owner_id)
) STRICT;

CREATE TABLE features (
  id           INTEGER PRIMARY KEY,
  report_id    TEXT NOT NULL,
  owner_id     INTEGER NOT NULL,
  position     INTEGER NOT NULL CHECK (position BETWEEN 1 AND 7),
  name         TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
  description  TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (report_id, position),
  UNIQUE (id, report_id),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE
) STRICT;

CREATE TABLE search_queries (
  id             INTEGER PRIMARY KEY,
  report_id      TEXT NOT NULL,
  owner_id       INTEGER NOT NULL,
  position       INTEGER NOT NULL CHECK (position BETWEEN 1 AND 14),
  source_type    TEXT NOT NULL CHECK (source_type IN ('github', 'hn', 'producthunt', 'arxiv', 'devpost', 'web')),
  query_text     TEXT NOT NULL CHECK (length(trim(query_text)) BETWEEN 1 AND 390),
  outcome        TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'ok', 'failed')),
  result_count   INTEGER CHECK (result_count >= 0),
  error_message  TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  CHECK ((outcome = 'ok') = (result_count IS NOT NULL)),
  CHECK ((outcome = 'failed') = (error_message IS NOT NULL AND length(trim(error_message)) > 0)),
  UNIQUE (report_id, position),
  UNIQUE (report_id, source_type, query_text),
  UNIQUE (id, report_id),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE
) STRICT;

CREATE TABLE candidates (
  id                 INTEGER PRIMARY KEY,
  report_id          TEXT NOT NULL,
  owner_id           INTEGER NOT NULL,
  found_by_query_id  INTEGER NOT NULL,
  url                TEXT NOT NULL CHECK (url GLOB 'http://?*' OR url GLOB 'https://?*'),
  -- Deliberate denormalization: url_key is url normalized (host without www + path, no trailing slash; the
  -- pipeline's norm_url). Stored so the database itself enforces one candidate per page per report.
  url_key            TEXT NOT NULL CHECK (length(url_key) > 0),
  title              TEXT NOT NULL DEFAULT '',
  snippet            TEXT NOT NULL DEFAULT '' CHECK (length(snippet) <= 600),
  search_score       REAL NOT NULL DEFAULT 0 CHECK (search_score >= 0),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (report_id, url_key),
  UNIQUE (id, report_id),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (found_by_query_id, report_id) REFERENCES search_queries (id, report_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE projects (
  id                 INTEGER PRIMARY KEY,
  report_id          TEXT NOT NULL,
  owner_id           INTEGER NOT NULL,
  candidate_id       INTEGER NOT NULL UNIQUE,
  citation_number    INTEGER CHECK (citation_number >= 1),
  name               TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
  one_liner          TEXT NOT NULL DEFAULT '',
  kind               TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('repo', 'product', 'paper', 'hackathon', 'other')),
  page_source        TEXT NOT NULL CHECK (page_source IN ('page', 'snippet')),
  injection_excerpt  TEXT CHECK (injection_excerpt IS NULL OR length(injection_excerpt) BETWEEN 1 AND 60),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (report_id, citation_number),
  UNIQUE (id, report_id),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (candidate_id, report_id) REFERENCES candidates (id, report_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE overlaps (
  id             INTEGER PRIMARY KEY,
  -- Deliberate denormalization: report_id repeats what project_id and feature_id imply. It is what lets the two
  -- composite foreign keys below prove that the project and the feature belong to the same report.
  report_id      TEXT NOT NULL,
  owner_id       INTEGER NOT NULL,
  project_id     INTEGER NOT NULL,
  feature_id     INTEGER NOT NULL,
  claimed_score  INTEGER NOT NULL CHECK (claimed_score BETWEEN 0 AND 2),
  final_score    INTEGER NOT NULL CHECK (final_score BETWEEN 0 AND 2),
  quote          TEXT,
  check_status   TEXT NOT NULL
                 CHECK (check_status IN ('none', 'unverified', 'verified', 'downgraded', 'cut_quote', 'cut_verifier')),
  check_note     TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (project_id, feature_id),
  CHECK (final_score <= claimed_score),
  CHECK ((claimed_score = 0) = (check_status = 'none')),
  CHECK ((claimed_score = 0) = (quote IS NULL)),
  CHECK (quote IS NULL OR length(trim(quote)) > 0),
  CHECK (check_status NOT IN ('cut_quote', 'cut_verifier') OR final_score = 0),
  CHECK (check_status NOT IN ('verified', 'unverified') OR final_score = claimed_score),
  CHECK (check_status <> 'downgraded' OR (final_score > 0 AND final_score < claimed_score)),
  CHECK ((check_status IN ('cut_quote', 'cut_verifier', 'downgraded')) = (length(trim(check_note)) > 0)),
  CHECK (check_status NOT IN ('verified', 'unverified', 'downgraded')
         OR length(trim(quote)) - length(replace(trim(quote), ' ', '')) >= 2),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE,
  FOREIGN KEY (project_id, report_id) REFERENCES projects (id, report_id) ON DELETE CASCADE,
  FOREIGN KEY (feature_id, report_id) REFERENCES features (id, report_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE pivots (
  id           INTEGER PRIMARY KEY,
  report_id    TEXT NOT NULL,
  owner_id     INTEGER NOT NULL,
  position     INTEGER NOT NULL CHECK (position BETWEEN 1 AND 3),
  title        TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
  pitch        TEXT NOT NULL DEFAULT '',
  why_open     TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (report_id, position),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE
) STRICT;

-- Deliberate denormalization: payloads repeat data held in the tables above. This is the append-only replay log
-- that lets a reopened page resume a running check (docs/USER_JOURNEYS.md J5); the tables stay the source of truth.
CREATE TABLE report_events (
  id           INTEGER PRIMARY KEY,
  report_id    TEXT NOT NULL,
  owner_id     INTEGER NOT NULL,
  seq          INTEGER NOT NULL CHECK (seq >= 1),
  kind         TEXT NOT NULL
               CHECK (kind IN ('stage', 'plan', 'search', 'triage', 'compared', 'verified', 'warn', 'error', 'done')),
  payload      TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object'),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
  UNIQUE (report_id, seq),
  FOREIGN KEY (report_id, owner_id) REFERENCES reports (id, owner_id) ON DELETE CASCADE ON UPDATE CASCADE
) STRICT;

CREATE INDEX reports_owner_recent ON reports (owner_id, created_at DESC);
CREATE INDEX reports_pivot_of ON reports (pivot_of_report_id) WHERE pivot_of_report_id IS NOT NULL;
CREATE INDEX candidates_query ON candidates (found_by_query_id, report_id);
CREATE INDEX projects_candidate ON projects (candidate_id, report_id);
CREATE INDEX overlaps_feature ON overlaps (feature_id, report_id);
CREATE INDEX overlaps_project ON overlaps (project_id, report_id);
CREATE INDEX features_report_owner ON features (report_id, owner_id);
CREATE INDEX search_queries_report_owner ON search_queries (report_id, owner_id);
CREATE INDEX candidates_report_owner ON candidates (report_id, owner_id);
CREATE INDEX projects_report_owner ON projects (report_id, owner_id);
CREATE INDEX overlaps_report_owner ON overlaps (report_id, owner_id);
CREATE INDEX pivots_report_owner ON pivots (report_id, owner_id);
CREATE INDEX report_events_report_owner ON report_events (report_id, owner_id);

-- ---------- derived data (views, so it can never disagree with the facts) ----------
CREATE VIEW feature_coverage AS
SELECT f.report_id, f.id AS feature_id, f.position,
       COALESCE(MAX(o.final_score), 0) AS best_score,
       COUNT(CASE WHEN o.final_score >= 1 THEN 1 END) AS projects_covering
FROM features f LEFT JOIN overlaps o ON o.feature_id = f.id
GROUP BY f.id;

CREATE VIEW report_gaps AS
SELECT report_id, feature_id, position FROM feature_coverage WHERE best_score = 0;

CREATE VIEW report_check_totals AS
SELECT r.id AS report_id,
       COUNT(CASE WHEN o.claimed_score > 0 THEN 1 END) AS checked,
       COUNT(CASE WHEN o.check_status = 'cut_quote' THEN 1 END) AS cut_quote,
       COUNT(CASE WHEN o.check_status = 'cut_verifier' THEN 1 END) AS cut_verifier,
       COUNT(CASE WHEN o.check_status = 'downgraded' THEN 1 END) AS downgraded,
       COUNT(CASE WHEN o.check_status = 'unverified' THEN 1 END) AS unverified
FROM reports r LEFT JOIN overlaps o ON o.report_id = r.id
GROUP BY r.id;

CREATE VIEW report_candidate_counts AS
SELECT r.id AS report_id, COUNT(c.id) AS candidates_found
FROM reports r LEFT JOIN candidates c ON c.report_id = r.id GROUP BY r.id;

-- ---------- invariants that span rows (triggers) ----------
CREATE TRIGGER reports_idea_immutable BEFORE UPDATE OF idea ON reports
WHEN NEW.idea IS NOT OLD.idea
BEGIN SELECT RAISE(ABORT, 'idea cannot change after a check starts'); END;

CREATE TRIGGER reports_finished_immutable
BEFORE UPDATE OF status, status_message, novelty, verdict, summary, target_user, writer_model, verifier_model,
                 simulate, started_at, finished_at ON reports
WHEN OLD.status <> 'running'
BEGIN SELECT RAISE(ABORT, 'a finished report cannot change'); END;

CREATE TRIGGER reports_finish_rules BEFORE UPDATE OF status ON reports
WHEN OLD.status = 'running' AND NEW.status <> 'running'
BEGIN
  SELECT RAISE(ABORT, 'a scored report needs at least one feature')
  WHERE NEW.status IN ('ok', 'no_close_match')
    AND NOT EXISTS (SELECT 1 FROM features WHERE report_id = NEW.id);
  SELECT RAISE(ABORT, 'every project needs a citation number when the report finishes')
  WHERE EXISTS (SELECT 1 FROM projects WHERE report_id = NEW.id AND citation_number IS NULL);
  SELECT RAISE(ABORT, 'citation numbers must run 1..N without gaps')
  WHERE (SELECT COALESCE(MAX(citation_number), 0) FROM projects WHERE report_id = NEW.id)
        <> (SELECT COUNT(*) FROM projects WHERE report_id = NEW.id);
  SELECT RAISE(ABORT, 'every project needs an overlap for every feature')
  WHERE NEW.status IN ('ok', 'no_close_match')
    AND (SELECT COUNT(*) FROM overlaps WHERE report_id = NEW.id)
        <> (SELECT COUNT(*) FROM projects WHERE report_id = NEW.id)
         * (SELECT COUNT(*) FROM features WHERE report_id = NEW.id);
  SELECT RAISE(ABORT, 'status ok needs at least one checked overlap')
  WHERE NEW.status = 'ok' AND NOT EXISTS (SELECT 1 FROM overlaps WHERE report_id = NEW.id AND final_score > 0);
  SELECT RAISE(ABORT, 'status no_close_match cannot have a checked overlap')
  WHERE NEW.status = 'no_close_match' AND EXISTS (SELECT 1 FROM overlaps WHERE report_id = NEW.id AND final_score > 0);
  SELECT RAISE(ABORT, 'a report without a score cannot have pivots')
  WHERE NEW.status IN ('failed', 'insufficient_evidence') AND EXISTS (SELECT 1 FROM pivots WHERE report_id = NEW.id);
  SELECT RAISE(ABORT, 'novelty must match checked coverage')
  WHERE NEW.novelty IS NOT NULL
    AND abs(NEW.novelty - 100.0 * (1.0 - (SELECT SUM(best_score) FROM feature_coverage WHERE report_id = NEW.id)
                                      / (2.0 * (SELECT COUNT(*) FROM features WHERE report_id = NEW.id)))) > 0.5;
END;

CREATE TRIGGER report_events_append_only BEFORE UPDATE ON report_events
BEGIN SELECT RAISE(ABORT, 'report events are append-only'); END;

CREATE TRIGGER features_insert_while_running BEFORE INSERT ON features
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: features are read-only'); END;
CREATE TRIGGER features_update_while_running BEFORE UPDATE OF position, name, description ON features
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: features are read-only'); END;

CREATE TRIGGER search_queries_insert_while_running BEFORE INSERT ON search_queries
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: search_queries are read-only'); END;
CREATE TRIGGER search_queries_update_while_running BEFORE UPDATE OF position, source_type, query_text, outcome, result_count, error_message ON search_queries
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: search_queries are read-only'); END;

CREATE TRIGGER candidates_insert_while_running BEFORE INSERT ON candidates
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: candidates are read-only'); END;
CREATE TRIGGER candidates_update_while_running BEFORE UPDATE OF found_by_query_id, url, url_key, title, snippet, search_score ON candidates
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: candidates are read-only'); END;

CREATE TRIGGER projects_insert_while_running BEFORE INSERT ON projects
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: projects are read-only'); END;
CREATE TRIGGER projects_update_while_running BEFORE UPDATE OF candidate_id, citation_number, name, one_liner, kind, page_source, injection_excerpt ON projects
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: projects are read-only'); END;

CREATE TRIGGER overlaps_insert_while_running BEFORE INSERT ON overlaps
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: overlaps are read-only'); END;
CREATE TRIGGER overlaps_update_while_running BEFORE UPDATE OF project_id, feature_id, claimed_score, final_score, quote, check_status, check_note ON overlaps
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: overlaps are read-only'); END;

CREATE TRIGGER pivots_insert_while_running BEFORE INSERT ON pivots
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: pivots are read-only'); END;
CREATE TRIGGER pivots_update_while_running BEFORE UPDATE OF position, title, pitch, why_open ON pivots
WHEN (SELECT status FROM reports WHERE id = OLD.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: pivots are read-only'); END;

CREATE TRIGGER report_events_insert_while_running BEFORE INSERT ON report_events
WHEN (SELECT status FROM reports WHERE id = NEW.report_id) <> 'running'
BEGIN SELECT RAISE(ABORT, 'report is finished: report_events are read-only'); END;

CREATE TRIGGER owners_touch AFTER UPDATE ON owners
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE owners SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER reports_touch AFTER UPDATE ON reports
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE reports SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER features_touch AFTER UPDATE ON features
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE features SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER search_queries_touch AFTER UPDATE ON search_queries
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE search_queries SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER candidates_touch AFTER UPDATE ON candidates
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE candidates SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER projects_touch AFTER UPDATE ON projects
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE projects SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER overlaps_touch AFTER UPDATE ON overlaps
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE overlaps SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

CREATE TRIGGER pivots_touch AFTER UPDATE ON pivots
WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE pivots SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;

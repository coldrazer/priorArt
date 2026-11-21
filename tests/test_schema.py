"""Invariants I1–I28 from docs/DATA_MODEL.md, checked against a database built by the real migrations."""
import sqlite3
import time

import pytest

from priorart import factories as F
from priorart.db import MIGRATIONS_DIR, connect
from priorart.migrate import Migrator

H = "a" * 64


def db():
    c = connect(":memory:")
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    return c

def seed(c, rid="AbCdEf1234", n_feats=2):
    """Owner 1 with one running report: n_feats features, one query, one candidate, project 1 (citation [1])."""
    F.owner(c, id=1, token="test-owner-1")
    F.report(c, 1, id=rid, idea="A research agent that checks ideas", writer_model="w/m", verifier_model="v/m")
    for i in range(1, n_feats + 1):
        F.feature(c, rid, i, f"F{i}", id=i)
    F.query(c, rid, 1, "github", "q", result_count=3, id=1)
    F.candidate(c, rid, 1, "https://github.com/a/b", id=1)
    F.project(c, rid, 1, "B", citation_number=1, page_source="page", id=1)
    return rid

def ov(c, fid, claimed, final, status, quote=None, note="", rid="AbCdEf1234", pid=1):
    """Raw insert on purpose: these tests feed deliberately invalid overlaps, which F.overlap would correct."""
    c.execute("INSERT INTO overlaps (report_id, owner_id, project_id, feature_id, claimed_score, final_score, quote, check_status, check_note) VALUES (?,1,?,?,?,?,?,?,?)",
              (rid, pid, fid, claimed, final, quote, status, note))

def finish(c, status="ok", novelty=50, msg="", rid="AbCdEf1234"):
    F.finish(c, rid, status, novelty=novelty, message=msg, summary="s")

def bad(c, sql, args=()):
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(sql, args)

# ---- happy path
def test_valid_report_lifecycle():
    c = db(); seed(c)
    ov(c, 1, 2, 2, "verified", "it clearly does this thing")
    ov(c, 2, 0, 0, "none")
    c.execute("INSERT INTO pivots (report_id, owner_id, position, title) VALUES ('AbCdEf1234', 1, 1, 'P')")
    finish(c, "ok", 50)  # coverage 2 of 4 -> 50
    r = c.execute("SELECT * FROM report_check_totals").fetchone()
    assert r[1:] == (1, 0, 0, 0, 0)
    assert c.execute("SELECT position FROM report_gaps").fetchall() == [(2,)]
    assert c.execute("SELECT candidates_found FROM report_candidate_counts").fetchone() == (1,)

# ---- report-level invariants
def test_idea_length_and_report_id_format():
    c = db(); c.execute("INSERT INTO owners (id, token_hash) VALUES (1, ?)", (H,))
    bad(c, "INSERT INTO reports (id, owner_id, idea, writer_model, verifier_model) VALUES ('AbCdEf1234',1,'short','w','v')")
    bad(c, "INSERT INTO reports (id, owner_id, idea, writer_model, verifier_model) VALUES ('AbCdEf1234',1,?, 'w','v')", ("x" * 4001,))
    bad(c, "INSERT INTO reports (id, owner_id, idea, writer_model, verifier_model) VALUES ('bad id!!!!',1,'long enough idea','w','v')")
    bad(c, "INSERT INTO reports (id, owner_id, idea, writer_model, verifier_model) VALUES ('AbCdEf1234',1,'long enough idea','','v')")

def test_owner_token_hash_format_and_unique():
    c = db()
    bad(c, "INSERT INTO owners (token_hash) VALUES ('plaintext-token')")
    c.execute("INSERT INTO owners (token_hash) VALUES (?)", (H,))
    bad(c, "INSERT INTO owners (token_hash) VALUES (?)", (H,))

def test_running_has_no_finish_time_and_finished_has_one():
    c = db(); seed(c)
    bad(c, "UPDATE reports SET status='failed', status_message='x' WHERE id='AbCdEf1234'")  # no finished_at

def test_score_presence_follows_status():
    c = db(); seed(c); ov(c, 1, 0, 0, "none"); ov(c, 2, 0, 0, "none")
    with pytest.raises(sqlite3.IntegrityError):
        finish(c, "failed", 40, "all searches failed")        # failed with a score
    with pytest.raises(sqlite3.IntegrityError):
        finish(c, "no_close_match", None, "none overlap")     # scored status without a score

def test_unhappy_status_must_explain():
    c = db(); seed(c)
    with pytest.raises(sqlite3.IntegrityError):
        finish(c, "failed", None, "")

def test_finished_report_is_immutable_but_deletable():
    c = db(); seed(c); ov(c, 1, 0, 0, "none"); ov(c, 2, 0, 0, "none")
    finish(c, "no_close_match", 100, "nothing overlapped")
    bad(c, "UPDATE reports SET novelty=10 WHERE id='AbCdEf1234'")
    bad(c, "UPDATE reports SET idea='a completely different idea' WHERE id='AbCdEf1234'")
    bad(c, "INSERT INTO features (report_id, owner_id, position, name) VALUES ('AbCdEf1234',1,3,'late')")
    bad(c, "UPDATE overlaps SET final_score=0 WHERE feature_id=1")
    c.execute("DELETE FROM reports WHERE id='AbCdEf1234'")
    for t in ["features","search_queries","candidates","projects","overlaps","pivots","report_events"]:
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t

def test_novelty_must_match_coverage():
    c = db(); seed(c); ov(c, 1, 2, 2, "verified", "it clearly does this"); ov(c, 2, 0, 0, "none")
    with pytest.raises(sqlite3.IntegrityError, match="novelty"):
        finish(c, "ok", 90)

def test_ok_needs_overlap_and_no_close_match_cannot_have_one():
    c = db(); seed(c); ov(c, 1, 0, 0, "none"); ov(c, 2, 0, 0, "none")
    with pytest.raises(sqlite3.IntegrityError, match="status ok"):
        finish(c, "ok", 100)
    c2 = db(); seed(c2); ov(c2, 1, 2, 2, "verified", "it clearly does this"); ov(c2, 2, 0, 0, "none")
    with pytest.raises(sqlite3.IntegrityError, match="no_close_match"):
        finish(c2, "no_close_match", 50, "x")

def test_grid_must_be_complete_and_citations_contiguous():
    c = db(); seed(c); ov(c, 1, 2, 2, "verified", "it clearly does this")   # feature 2 missing
    with pytest.raises(sqlite3.IntegrityError, match="every feature"):
        finish(c, "ok", 50)
    c2 = db(); seed(c2); c2.execute("UPDATE projects SET citation_number=2 WHERE id=1")
    ov(c2, 1, 2, 2, "verified", "it clearly does this"); ov(c2, 2, 0, 0, "none")
    with pytest.raises(sqlite3.IntegrityError, match="without gaps"):
        finish(c2, "ok", 50)

def test_failed_report_cannot_have_pivots():
    c = db(); seed(c)
    c.execute("INSERT INTO pivots (report_id, owner_id, position, title) VALUES ('AbCdEf1234', 1, 1, 'P')")
    with pytest.raises(sqlite3.IntegrityError, match="pivots"):
        finish(c, "failed", None, "comparisons failed")

# ---- overlap (evidence) invariants
@pytest.mark.parametrize("claimed,final,status,quote,note", [
    (2, 2, "none", None, ""),                         # claim without status
    (0, 0, "verified", "a b c", ""),                  # verified with no claim
    (2, 2, "verified", None, ""),                     # claim without quote
    (1, 2, "verified", "a b c", ""),                  # final above claimed
    (2, 1, "verified", "a b c", ""),                  # verified must keep the claimed score
    (2, 1, "cut_quote", "a b c", "not on page"),      # cut must zero the score
    (2, 0, "cut_verifier", "a b c", ""),              # rejection without a reason
    (2, 2, "downgraded", "a b c", "weaker"),          # downgrade must lower the score
    (2, 2, "verified", "two words", ""),              # evidence quotes need 3+ words
    (0, 0, "none", "stray quote", ""),                # no claim, no quote
])
def test_overlap_rules(claimed, final, status, quote, note):
    c = db(); seed(c)
    with pytest.raises(sqlite3.IntegrityError):
        ov(c, 1, claimed, final, status, quote, note)

def test_overlap_feature_and_project_must_share_report():
    c = db(); seed(c, "AbCdEf1234")
    c.execute("INSERT INTO reports (id, owner_id, idea, writer_model, verifier_model) VALUES ('ZzZzZz9999', 1, 'Another idea entirely', 'w', 'v')")
    c.execute("INSERT INTO features (id, report_id, owner_id, position, name) VALUES (9, 'ZzZzZz9999', 1, 1, 'X')")
    with pytest.raises(sqlite3.IntegrityError):
        ov(c, 9, 0, 0, "none")   # project 1 is in AbCdEf1234, feature 9 in ZzZzZz9999

def test_child_owner_must_match_report_owner():
    c = db(); seed(c); c.execute("INSERT INTO owners (id, token_hash) VALUES (2, ?)", ("b" * 64,))
    bad(c, "INSERT INTO pivots (report_id, owner_id, position, title) VALUES ('AbCdEf1234', 2, 1, 'P')")

def test_one_candidate_per_page_and_one_project_per_candidate():
    c = db(); seed(c)
    bad(c, "INSERT INTO candidates (report_id, owner_id, found_by_query_id, url, url_key) VALUES ('AbCdEf1234',1,1,'https://www.github.com/a/b/','github.com/a/b')")
    bad(c, "INSERT INTO projects (report_id, owner_id, candidate_id, name, page_source) VALUES ('AbCdEf1234',1,1,'dup','page')")
    bad(c, "INSERT INTO candidates (report_id, owner_id, found_by_query_id, url, url_key) VALUES ('AbCdEf1234',1,1,'ftp://x','x')")

def test_query_outcome_rules():
    c = db(); seed(c)
    bad(c, "INSERT INTO search_queries (report_id, owner_id, position, source_type, query_text, outcome) VALUES ('AbCdEf1234',1,2,'github','q2','failed')")
    bad(c, "INSERT INTO search_queries (report_id, owner_id, position, source_type, query_text, outcome) VALUES ('AbCdEf1234',1,2,'twitter','q2','pending')")
    bad(c, "INSERT INTO search_queries (report_id, owner_id, position, source_type, query_text) VALUES ('AbCdEf1234',1,15,'web','q2')")

def test_events_append_only_and_json():
    c = db(); seed(c)
    c.execute("INSERT INTO report_events (report_id, owner_id, seq, kind, payload) VALUES ('AbCdEf1234',1,1,'stage','{\"stage\":\"search\"}')")
    bad(c, "UPDATE report_events SET payload='{}' WHERE seq=1")
    bad(c, "INSERT INTO report_events (report_id, owner_id, seq, kind, payload) VALUES ('AbCdEf1234',1,2,'stage','not json')")
    bad(c, "INSERT INTO report_events (report_id, owner_id, seq, kind, payload) VALUES ('AbCdEf1234',1,1,'warn','{}')")

def test_pivot_of_survives_parent_deletion_and_cannot_be_self():
    c = db(); seed(c)
    c.execute("INSERT INTO reports (id, owner_id, pivot_of_report_id, idea, writer_model, verifier_model) VALUES ('Child00001',1,'AbCdEf1234','A pivot of the first idea','w','v')")
    bad(c, "UPDATE reports SET pivot_of_report_id='Child00001' WHERE id='Child00001'")
    c.execute("DELETE FROM reports WHERE id='AbCdEf1234'")
    assert c.execute("SELECT pivot_of_report_id FROM reports WHERE id='Child00001'").fetchone() == (None,)

def test_owner_delete_cascades_and_owner_move_cascades_after_finish():
    c = db(); seed(c); ov(c, 1, 0, 0, "none"); ov(c, 2, 0, 0, "none")
    finish(c, "no_close_match", 100, "nothing overlapped")
    c.execute("INSERT INTO owners (id, token_hash) VALUES (2, ?)", ("c" * 64,))
    c.execute("UPDATE reports SET owner_id=2 WHERE id='AbCdEf1234'")      # e.g. claiming reports on sign-in
    assert c.execute("SELECT DISTINCT owner_id FROM overlaps").fetchall() == [(2,)]
    c.execute("DELETE FROM owners WHERE id=2")
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone() == (0,)

def test_timestamps_utc_format_and_updated_at_moves():
    c = db(); seed(c)
    bad(c, "UPDATE features SET created_at='2026-11-19 20:00:00' WHERE id=1")
    before = c.execute("SELECT updated_at FROM features WHERE id=1").fetchone()[0]
    time.sleep(0.01)
    c.execute("UPDATE features SET name='renamed' WHERE id=1")
    after = c.execute("SELECT updated_at FROM features WHERE id=1").fetchone()[0]
    assert after > before and after.endswith("Z")

def test_strict_types():
    c = db(); seed(c)
    bad(c, "UPDATE features SET position='one' WHERE id=1")

"""Record lifecycle (03.5): soft delete, purge, audit, owner deletion, export, backups, pool, no orphans."""
import json
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from priorart import factories as F
from priorart import lifecycle as L
from priorart.db import MIGRATIONS_DIR, Pool, PoolTimeout, connect
from priorart.migrate import Migrator
from priorart.queries import QUERIES, params_for
from priorart.seed import seed

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
CHILD_TABLES = ["features", "search_queries", "candidates", "projects", "overlaps", "pivots", "report_events"]


def fresh(path=":memory:"):
    c = connect(path)
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    return c


def audit(c, action=None):
    sql = "SELECT actor, action, owner_ref, report_ref, before, after FROM audit_events"
    rows = c.execute(sql + (" WHERE action = ?" if action else "") + " ORDER BY id", (action,) if action else ()).fetchall()
    return [(a, act, o, r, json.loads(b) if b else None, json.loads(af) if af else None) for a, act, o, r, b, af in rows]


def one_report(c, token="t"):
    o = F.owner(c, token=token)
    r = F.build_report(c, o, at=NOW - timedelta(days=60),
                       projects=[{"name": "X", "url": "https://example.com/x", "cells": {1: (2,)}}],
                       pivots=["P"], events=[("stage", {}), ("done", {})])
    return o, r


# ---------- soft delete, restore, purge

def test_soft_delete_hides_from_every_app_query_and_restore_brings_it_back():
    c = fresh()
    o, r = one_report(c)
    assert L.delete_report(c, r, o, now=NOW)
    assert c.execute(QUERIES["report_by_id"].sql, (r,)).fetchone() is None
    assert c.execute(QUERIES["recent_reports"].sql, params_for(QUERIES["recent_reports"], o)).fetchall() == []
    assert [x[0] for x in c.execute(QUERIES["recently_deleted"].sql, params_for(QUERIES["recently_deleted"], o))] == [r]
    assert c.execute("SELECT COUNT(*) FROM overlaps WHERE report_id = ?", (r,)).fetchone()[0] > 0  # kept for restore
    assert L.restore_report(c, r, o, now=NOW + timedelta(days=29))
    assert c.execute(QUERIES["report_by_id"].sql, (r,)).fetchone()[0] == r
    assert [a[1] for a in audit(c)] == ["report_deleted", "report_restored"]


def test_only_the_owner_can_delete_and_running_checks_cannot_be_deleted():
    c = fresh()
    o, r = one_report(c)
    other = F.owner(c, token="other")
    assert not L.delete_report(c, r, other, now=NOW)
    running = F.build_report(c, o, status=None)
    with pytest.raises(sqlite3.IntegrityError, match="running check"):
        L.delete_report(c, running, o, now=NOW)


def test_restore_window_and_purge():
    c = fresh()
    o, r = one_report(c)
    o2, keep = one_report(c, token="t2")
    L.delete_report(c, r, o, now=NOW)
    L.delete_report(c, keep, o2, now=NOW + timedelta(days=10))
    assert not L.restore_report(c, r, o, now=NOW + timedelta(days=31))       # window closed
    assert L.purge_expired(c, now=NOW + timedelta(days=31)) == 1
    assert c.execute("SELECT COUNT(*) FROM reports WHERE id = ?", (r,)).fetchone() == (0,)
    for t in CHILD_TABLES:
        assert c.execute(f"SELECT COUNT(*) FROM {t} WHERE report_id = ?", (r,)).fetchone() == (0,), t
    assert c.execute("SELECT deleted_at IS NOT NULL FROM reports WHERE id = ?", (keep,)).fetchone() == (1,)
    purged = audit(c, "report_purged")
    assert purged[0][0] == "purge" and purged[0][3] == r and purged[0][4]["status"] == "ok" and purged[0][5] is None


def test_purge_works_in_batches():
    c = fresh()
    o = F.owner(c, token="many")
    ids = [F.build_report(c, o, at=NOW - timedelta(days=90)) for _ in range(250)]
    for rid in ids:
        L.delete_report(c, rid, o, now=NOW - timedelta(days=40))
    assert L.purge_expired(c, now=NOW) == 250
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone() == (0,)


# ---------- audit trail

def test_audit_records_who_what_when_before_after():
    c = fresh()
    o, r = one_report(c)
    L.delete_report(c, r, o, now=NOW, actor="operator")
    actor, action, owner_ref, report_ref, before, after = audit(c)[0]
    assert (actor, action, owner_ref, report_ref) == ("operator", "report_deleted", o, r)
    assert before == {"deleted_at": None} and after == {"deleted_at": "2026-11-20T12:00:00.000Z"}
    when = c.execute("SELECT created_at FROM audit_events").fetchone()[0]
    assert when.endswith("Z")


def test_audit_is_append_only_and_raw_sql_is_attributed():
    c = fresh()
    o, r = one_report(c)
    c.execute("UPDATE reports SET deleted_at = ? WHERE id = ?", (F.ts(NOW), r))  # bypasses lifecycle.py
    assert audit(c)[0][:2] == ("sql", "report_deleted")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE audit_events SET actor = 'owner'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        c.execute("DELETE FROM audit_events")


def test_audit_holds_no_content():
    c = fresh()
    o, r = one_report(c)
    idea = c.execute("SELECT idea FROM reports WHERE id = ?", (r,)).fetchone()[0]
    L.delete_report(c, r, o, now=NOW)
    L.delete_owner(c, o)
    dump = json.dumps(c.execute("SELECT * FROM audit_events").fetchall())
    assert idea not in dump and "example.com" not in dump


def test_owner_reassignment_is_audited():
    c = fresh()
    o, r = one_report(c)
    new = F.owner(c, token="claimed")
    with L.transaction(c, "system"):
        c.execute("UPDATE reports SET owner_id = ? WHERE id = ?", (new, r))
    a = audit(c, "owner_reassigned")[0]
    assert a[0] == "system" and a[4] == {"owner_id": o} and a[5] == {"owner_id": new}
    assert c.execute("SELECT DISTINCT owner_id FROM overlaps WHERE report_id = ?", (r,)).fetchall() == [(new,)]


# ---------- owner deletion and export

def test_delete_owner_removes_everything_and_only_theirs():
    c = fresh()
    seed(c)
    o, r = one_report(c)
    keep = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id <> ?", (o,)).fetchone()[0] for t in ["reports"] + CHILD_TABLES}
    counts = L.delete_owner(c, o, actor="owner")
    assert counts["reports"] == 1 and counts["overlaps"] == 3
    for t in ["reports"] + CHILD_TABLES:
        assert c.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id = ?", (o,)).fetchone() == (0,), t
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == keep[t], t
    assert c.execute("SELECT COUNT(*) FROM owners WHERE id = ?", (o,)).fetchone() == (0,)
    assert [a[1] for a in audit(c) if a[2] == o] == ["owner_deleted", "report_purged"]
    with pytest.raises(L.LifecycleError):
        L.delete_owner(c, o)


def test_export_contains_all_of_an_owners_data_and_nothing_else():
    c = fresh()
    seed(c)
    o = 900002  # the "typical" seeded owner: one report per status, a pivot, a running check
    L.delete_report(c, "SdTypNcm01", o, now=NOW)
    data = L.export_owner(c, o)
    ids = {r["id"] for r in data["reports"]}
    assert ids == {r for (r,) in c.execute("SELECT id FROM reports WHERE owner_id = ?", (o,))}  # deleted one included
    ok = next(r for r in data["reports"] if r["id"] == "SdTypOk001")
    assert len(ok["features"]) == 4 and len(ok["projects"]) == 3 and len(ok["overlaps"]) == 12 and len(ok["pivots"]) == 3
    assert ok["projects"][0]["url"] == "https://github.com/example/policydiff"
    assert all("SdHvy" not in r["id"] for r in data["reports"])
    assert {a["action"] for a in data["audit"]} >= {"report_deleted"}
    assert audit(c, "owner_exported")[-1][2] == o
    json.dumps(data)  # serializable


def test_export_pages_through_large_owners_without_losing_reports():
    c = fresh()
    o = F.owner(c, token="big")
    same_time = NOW - timedelta(days=5)
    made = {F.build_report(c, o, at=same_time) for _ in range(230)}   # identical created_at: tests tie-breaking
    assert {r["id"] for r in L.export_owner(c, o)["reports"]} == made


# ---------- no orphans

def test_every_foreign_key_cascades_or_nulls():
    """Static half of the proof: with foreign keys enforced on every connection (db.connect), deleting any
    parent row either deletes its children (CASCADE) or clears a nullable reference (SET NULL). No FK can leave
    a child pointing at a missing parent, and none is NO ACTION / RESTRICT (which would just block the delete)."""
    c = fresh()
    assert c.execute("PRAGMA foreign_keys").fetchone() == (1,)
    tables = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    seen = 0
    for t in tables:
        cols = {row[1]: row for row in c.execute(f"PRAGMA table_info({t})")}
        for fk in c.execute(f"PRAGMA foreign_key_list({t})"):
            seen += 1
            on_delete = fk[6]
            assert on_delete in ("CASCADE", "SET NULL"), (t, fk)
            if on_delete == "SET NULL":
                assert cols[fk[3]][3] == 0, f"{t}.{fk[3]} is SET NULL but NOT NULL"
    assert seen >= 20


@pytest.mark.parametrize("action", ["delete_report", "purge", "delete_owner", "reseed", "reassign"])
def test_no_lifecycle_path_leaves_orphans(action):
    """Dynamic half: run each lifecycle path on the full seed, then look for children without parents."""
    c = fresh()
    seed(c)
    if action == "delete_report":
        L.delete_report(c, "SdTypOk001", 900002, now=NOW)
    elif action == "purge":
        L.delete_report(c, "SdTypOk001", 900002, now=NOW)   # parent of the pivot report SdTypPvt01
        L.purge_expired(c, now=NOW + timedelta(days=31))
        assert c.execute("SELECT pivot_of_report_id FROM reports WHERE id = 'SdTypPvt01'").fetchone() == (None,)
    elif action == "delete_owner":
        L.delete_owner(c, 900003)
    elif action == "reseed":
        seed(c)
    else:
        new = F.owner(c, token="claim")
        with L.transaction(c, "system"):
            c.execute("UPDATE reports SET owner_id = ? WHERE owner_id = 900002", (new,))
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    for t in CHILD_TABLES:
        assert c.execute(f"SELECT COUNT(*) FROM {t} x LEFT JOIN reports r ON r.id = x.report_id "
                         "WHERE r.id IS NULL OR r.owner_id <> x.owner_id").fetchone() == (0,), t
    assert c.execute("SELECT COUNT(*) FROM reports r LEFT JOIN owners o ON o.id = r.owner_id WHERE o.id IS NULL").fetchone() == (0,)


def test_a_lifecycle_change_that_would_orphan_is_rolled_back():
    c = fresh()
    o, r = one_report(c)
    c.execute("PRAGMA foreign_keys = OFF")   # simulate a connection opened without db.connect
    with pytest.raises(L.LifecycleError, match="broken reference"):
        with L.transaction(c, "operator"):
            c.execute("DELETE FROM reports WHERE id = ?", (r,))
    c.execute("PRAGMA foreign_keys = ON")
    assert c.execute("SELECT COUNT(*) FROM reports WHERE id = ?", (r,)).fetchone() == (1,)


# ---------- backups

def test_backup_restore_round_trip(tmp_path):
    db = tmp_path / "live.sqlite"
    c = fresh(db)
    seed(c)
    c.close()
    before = L.database_fingerprint(db)
    info = L.backup(db, tmp_path / "b.sqlite")
    assert info["integrity"] == "ok" and info["schema_version"] == 5
    c = connect(db)
    L.delete_owner(c, 900003)
    c.close()
    assert L.database_fingerprint(db) != before
    L.restore(tmp_path / "b.sqlite", db)
    assert L.database_fingerprint(db) == before


def test_restore_refuses_a_corrupt_backup(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000)
    target = tmp_path / "t.sqlite"
    fresh(target).close()
    with pytest.raises((L.LifecycleError, sqlite3.DatabaseError)):
        L.restore(bad, target)
    assert L.verify(target)["integrity"] == "ok"   # target untouched


def test_backup_cli_keeps_newest_and_drill_passes(tmp_path):
    db = tmp_path / "x.sqlite"
    fresh(db).close()
    run = lambda *a: subprocess.run([sys.executable, "scripts/backup.py", "--db", str(db), "--dir", str(tmp_path / "bk"), *a],
                                    cwd=ROOT, capture_output=True, text=True)
    for _ in range(3):
        assert run("backup", "--keep", "2").returncode == 0
    files = list((tmp_path / "bk").glob("priorart-*.sqlite"))
    assert 1 <= len(files) <= 2
    assert run("restore", str(files[0])).returncode == 2           # needs --yes
    assert run("restore", str(files[0]), "--yes").returncode == 0
    drill = run("drill", "--owners", "20", "--reports-per-owner", "2")
    assert drill.returncode == 0 and "MATCHES" in drill.stdout


# ---------- connection pool

def test_pool_one_writer_parallel_read_only_readers(tmp_path):
    db = tmp_path / "p.sqlite"
    c = fresh(db)
    seed(c)
    c.close()
    pool = Pool(db, readers=3, timeout=0.5)
    with pool.read() as r:
        assert r.execute("PRAGMA foreign_keys").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            r.execute("DELETE FROM owners")             # query_only
    errors, counts = [], []

    def reader():
        try:
            for _ in range(50):
                with pool.read() as r:
                    counts.append(r.execute("SELECT COUNT(*) FROM reports").fetchone()[0])
        except Exception as e:
            errors.append(e)

    def writer():
        try:
            for _i in range(20):
                with pool.write() as w:
                    F.build_report(w, 900001)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with pool.read() as r:
        assert r.execute("SELECT COUNT(*) FROM reports WHERE owner_id = 900001").fetchone() == (40,)
    held = [pool._readers.get() for _ in range(3)]
    with pytest.raises(PoolTimeout):
        with pool.read():
            pass
    for h in held:
        pool._readers.put(h)
    pool.close()


def test_pool_rejects_memory_databases():
    with pytest.raises(Exception, match="file"):
        Pool(":memory:")


def test_data_cli_export_and_delete(tmp_path):
    db = tmp_path / "d.sqlite"
    c = fresh(db)
    seed(c)
    c.close()
    run = lambda *a: subprocess.run([sys.executable, "scripts/data.py", "--db", str(db), *a], cwd=ROOT,
                                    capture_output=True, text=True)
    out = tmp_path / "export.json"
    r = run("export", "--owner-token", "priorart-seed-typical", "--out", str(out))
    assert r.returncode == 0 and len(json.loads(out.read_text())["reports"]) == 6
    assert run("delete-owner", "--owner-token", "priorart-seed-typical").returncode == 2
    assert run("delete-owner", "--owner-token", "priorart-seed-typical", "--yes").returncode == 0
    assert run("export", "--owner-token", "priorart-seed-typical").returncode == 1
    assert run("purge").returncode == 0

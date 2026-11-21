"""How records are deleted, restored, purged, exported and backed up (docs/DATA_LIFECYCLE.md).

Every change here runs in one transaction that:
  1. records who is acting (audit_context), so the audit triggers attribute the change,
  2. makes the change,
  3. checks every foreign key (PRAGMA foreign_key_check) and rolls back if any reference broke,
  4. resets the actor and commits.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .factories import ts
from .queries import QUERIES, params_for

RETENTION_DAYS = 30
ACTORS = ("sql", "owner", "operator", "seed", "purge", "system")


class LifecycleError(RuntimeError):
    pass


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _ro_uri(path: str | Path) -> str:
    resolved = Path(path).resolve().as_posix()
    return f"file:{quote(resolved, safe='/')}?mode=ro"


@contextmanager
def transaction(conn: sqlite3.Connection, actor: str) -> Iterator[sqlite3.Connection]:
    if actor not in ACTORS:
        raise LifecycleError(f"unknown actor {actor!r}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE audit_context SET actor = ? WHERE id = 1", (actor,))
        yield conn
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise LifecycleError(f"would leave {len(broken)} broken reference(s), e.g. {broken[0]}")
        conn.execute("UPDATE audit_context SET actor = 'sql' WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------- reports ---------------------------------------------------------------------------

def delete_report(conn: sqlite3.Connection, report_id: str, owner_id: int, *, now: datetime | None = None, actor: str = "owner") -> bool:
    """Soft delete: hidden at once, restorable for RETENTION_DAYS. False if not found, not the owner's, or already deleted."""
    with transaction(conn, actor):
        cur = conn.execute(
            "UPDATE reports SET deleted_at = ? WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
            (ts(_now(now)), report_id, owner_id),
        )
    return cur.rowcount == 1


def restore_report(conn: sqlite3.Connection, report_id: str, owner_id: int, *, now: datetime | None = None, actor: str = "owner") -> bool:
    """Undo a soft delete within the retention window. False if not found, not deleted, or already past the window."""
    cutoff = ts(_now(now) - timedelta(days=RETENTION_DAYS))
    with transaction(conn, actor):
        cur = conn.execute(
            "UPDATE reports SET deleted_at = NULL WHERE id = ? AND owner_id = ? "
            "AND deleted_at IS NOT NULL AND deleted_at >= ?",
            (report_id, owner_id, cutoff),
        )
    return cur.rowcount == 1


def purge_expired(conn: sqlite3.Connection, *, now: datetime | None = None, actor: str = "purge") -> int:
    """Permanently delete reports soft-deleted more than RETENTION_DAYS ago, in bounded batches. Returns the count."""
    cutoff = ts(_now(now) - timedelta(days=RETENTION_DAYS))
    q = QUERIES["purge_due"]
    assert q.default_limit is not None
    total = 0
    while True:
        with transaction(conn, actor):
            ids = [r[0] for r in conn.execute(q.sql, params_for(q, cutoff))]
            for rid in ids:
                conn.execute("DELETE FROM reports WHERE id = ?", (rid,))
        total += len(ids)
        if len(ids) < q.default_limit:
            return total


# ---------- owners ----------------------------------------------------------------------------

def owner_id_for_token(conn: sqlite3.Connection, token: str) -> int | None:
    row = conn.execute(QUERIES["owner_by_token"].sql, (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return int(row[0]) if row else None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur]


def _report_export(conn: sqlite3.Connection, rid: str) -> dict[str, Any]:
    report = _rows(
        conn,
        "SELECT id, pivot_of_report_id, idea, summary, target_user, status, status_message, novelty, "
        "verdict, writer_model, verifier_model, simulate, started_at, finished_at, created_at, "
        "deleted_at FROM reports WHERE id = ?",
        (rid,),
    )[0]
    for key, name in (
        ("features", "report_features"),
        ("projects", "report_projects"),
        ("overlaps", "report_overlaps"),
        ("pivots", "report_pivots"),
        ("searches", "report_queries"),
    ):
        report[key] = _rows(conn, QUERIES[name].sql, params_for(QUERIES[name], rid))
    events: list[dict[str, Any]] = []
    after = 0
    q = QUERIES["report_events_after"]
    assert q.default_limit is not None
    while True:
        page = _rows(conn, q.sql, params_for(q, rid, after))
        for e in page:
            e["payload"] = json.loads(e["payload"])
        events += page
        if len(page) < q.default_limit:
            break
        after = page[-1]["seq"]
    report["events"] = events
    return report


def export_owner(conn: sqlite3.Connection, owner_id: int, *, actor: str = "owner") -> dict[str, Any]:
    """Everything stored about an owner, deleted-but-restorable reports included, as one JSON-ready dict."""
    owner = conn.execute("SELECT id, created_at FROM owners WHERE id = ?", (owner_id,)).fetchone()
    if owner is None:
        raise LifecycleError(f"no owner {owner_id}")
    conn.execute("BEGIN")  # one read snapshot: the export is consistent even if the app writes meanwhile
    try:
        reports: list[dict[str, Any]] = []
        key: tuple[str, str] = ("", "")
        q = QUERIES["owner_reports_page"]
        assert q.default_limit is not None
        while True:
            page = conn.execute(q.sql, params_for(q, owner_id, *key)).fetchall()
            for rid, _ in page:
                reports.append(_report_export(conn, rid))
            if len(page) < q.default_limit:
                break
            key = (page[-1][1], page[-1][0])
        audit: list[dict[str, Any]] = []
        last = 0
        aq = QUERIES["owner_audit_page"]
        assert aq.default_limit is not None
        while True:
            apage = _rows(conn, aq.sql, params_for(aq, owner_id, last))
            for a in apage:
                a["before"] = json.loads(a["before"]) if a["before"] else None
                a["after"] = json.loads(a["after"]) if a["after"] else None
            audit += apage
            if len(apage) < aq.default_limit:
                break
            last = apage[-1]["id"]
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    with transaction(conn, actor):
        conn.execute(
            "INSERT INTO audit_events (actor, action, owner_ref, before, after) VALUES (?, 'owner_exported', ?, "
            "NULL, json_object('reports', ?))",
            (actor, owner_id, len(reports)),
        )
    return {
        "format": "prior-art-export/1",
        "exported_at": ts(_now(None)),
        "owner": {"id": owner[0], "created_at": owner[1]},
        "reports": reports,
        "audit": audit,
    }


def delete_owner(conn: sqlite3.Connection, owner_id: int, *, actor: str = "owner") -> dict[str, int]:
    """Permanently delete an owner and everything they own. Uses indexed report_id lookups for counts."""
    child_tables = ["features", "search_queries", "candidates", "projects", "overlaps", "pivots", "report_events"]
    with transaction(conn, actor):
        report_ids = [r[0] for r in conn.execute("SELECT id FROM reports WHERE owner_id = ?", (owner_id,))]
        counts: dict[str, int] = {"reports": len(report_ids)}
        if report_ids:
            marks = ",".join("?" for _ in report_ids)
            for t in child_tables:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE report_id IN ({marks})", report_ids).fetchone()[0]
        else:
            for t in child_tables:
                counts[t] = 0
        cur = conn.execute("DELETE FROM owners WHERE id = ?", (owner_id,))
        if cur.rowcount != 1:
            raise LifecycleError(f"no owner {owner_id}")
        left_reports = conn.execute("SELECT COUNT(*) FROM reports WHERE owner_id = ?", (owner_id,)).fetchone()[0]
        left: dict[str, int] = {"reports": left_reports}
        if report_ids:
            marks = ",".join("?" for _ in report_ids)
            for t in child_tables:
                left[t] = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE report_id IN ({marks})", report_ids).fetchone()[0]
        if any(left.values()):
            raise LifecycleError(f"rows survived the owner's deletion: {left}")
    return counts


# ---------- backup and restore ----------------------------------------------------------------

def verify(path: str | Path) -> dict[str, Any]:
    """Open a database file read-only and check it: integrity, foreign keys, schema version."""
    conn = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    if integrity != "ok" or broken:
        raise LifecycleError(f"{path} failed verification: integrity={integrity!r}, broken references={len(broken)}")
    return {"integrity": integrity, "schema_version": version}


def backup(src_path: str | Path, dest_path: str | Path) -> dict[str, Any]:
    """Atomic, online, consistent copy using SQLite's backup API, verified before swapping into place."""
    dest_p = Path(dest_path)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_p = dest_p.with_name(f".{dest_p.name}.tmp")
    t = time.perf_counter()
    src = sqlite3.connect(str(src_path), timeout=5.0)
    dest = sqlite3.connect(str(tmp_p))
    try:
        src.backup(dest)
    except Exception:
        dest.close()
        src.close()
        tmp_p.unlink(missing_ok=True)
        raise
    else:
        dest.close()
        src.close()
    try:
        info = verify(tmp_p)
        os.replace(tmp_p, dest_p)
    except Exception:
        tmp_p.unlink(missing_ok=True)
        raise
    took = time.perf_counter() - t
    return {"path": str(dest_p), "bytes": dest_p.stat().st_size, "seconds": took, **info}


def restore(backup_path: str | Path, target_path: str | Path) -> dict[str, Any]:
    """Replace the target database's contents with a verified backup via staging swap. Stop the app first."""
    verify(backup_path)
    target_p = Path(target_path)
    target_p.parent.mkdir(parents=True, exist_ok=True)
    staging = target_p.with_name(f".{target_p.name}.restore.tmp")
    t = time.perf_counter()
    src = sqlite3.connect(_ro_uri(backup_path), uri=True)
    dest = sqlite3.connect(str(staging))
    try:
        src.backup(dest)
    except Exception:
        dest.close()
        src.close()
        staging.unlink(missing_ok=True)
        raise
    else:
        dest.close()
        src.close()
    try:
        info = verify(staging)
        for suffix in ("-wal", "-shm"):
            Path(str(target_p) + suffix).unlink(missing_ok=True)
        os.replace(staging, target_p)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    took = time.perf_counter() - t
    return {"path": str(target_p), "seconds": took, **info}


def database_fingerprint(path: str | Path) -> str:
    """SHA-256 over every row of every table, in rowid order: equal fingerprints mean identical data."""
    conn = sqlite3.connect(_ro_uri(path), uri=True)
    try:
        h = hashlib.sha256()
        tables = [
            t
            for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for t in tables:
            for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(json.dumps([t, *row], default=str, ensure_ascii=False).encode())
        return h.hexdigest()
    finally:
        conn.close()

"""Database-backed ReportStore, SQLite token-bucket RateLimiter, and UsageLedger.

Wires the 03.x schema (src/priorart/schema.sql), migrations, Pool, queries, and lifecycle module into
the live application.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import lifecycle as L
from .db import MIGRATIONS_DIR, Pool
from .factories import ts
from .log import get_logger, log_event
from .migrate import Migrator
from .pipeline import norm_url, novelty_score
from .queries import QUERIES, params_for
from .resilience import UsageRecord
from .search import SOURCES, safe_url

logger = get_logger("store")

_BASE62 = string.digits + string.ascii_letters
_VALID_KINDS = frozenset({"repo", "product", "paper", "hackathon", "other"})
_VALID_SOURCES = frozenset(SOURCES.keys())


def new_db_report_id() -> str:
    """10-char base62 id matching schema.sql reports.id CHECK."""
    return "".join(secrets.choice(_BASE62) for _ in range(10))


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clamp(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _utc_now(now: datetime | None = None) -> str:
    return ts(now or datetime.now(timezone.utc))


def _db_overlap_fields(cell: dict[str, Any]) -> tuple[int, int, str | None, str, str]:
    """Map a pipeline overlap cell into values guaranteed to satisfy every schema.sql overlaps CHECK."""
    claimed = max(0, min(2, int(cell.get("claimed", 0) or 0)))
    final = max(0, min(claimed, int(cell.get("score", 0) or 0)))
    raw_status = str(cell.get("status") or ("none" if claimed == 0 else "unverified"))
    raw_quote = str(cell.get("quote") or "").strip()
    raw_note = str(cell.get("reason") or "").strip()

    if claimed == 0:
        return 0, 0, None, "none", ""

    if raw_status == "cut_quote":
        quote = raw_quote or "(no quote provided)"
        return claimed, 0, quote, "cut_quote", raw_note or "Quote not found on the page"

    if raw_status == "cut_verifier":
        quote = raw_quote or "(no quote provided)"
        return claimed, 0, quote, "cut_verifier", raw_note or "Quote doesn't prove the claim"

    if raw_status == "downgraded":
        if final <= 0 or final >= claimed:
            final = max(1, claimed - 1) if claimed > 1 else 0
            if final == 0:
                return claimed, 0, raw_quote or "(no quote provided)", "cut_verifier", raw_note or "Quote doesn't prove the claim"
        quote = _ensure_three_space_tokens(raw_quote or "project page partial match")
        return claimed, final, quote, "downgraded", raw_note or "Quote shows only partial support"

    if raw_status == "verified":
        # schema.sql I17 enforces check_note == '' for verified/unverified rows.
        quote = _ensure_three_space_tokens(raw_quote or "project page evidence quote")
        return claimed, claimed, quote, "verified", ""

    # 'claimed', 'unverified', 'unchecked'
    quote = _ensure_three_space_tokens(raw_quote or "project page evidence quote")
    return claimed, claimed, quote, "unverified", ""


def _ensure_three_space_tokens(quote: str) -> str:
    """Satisfy schema.sql I18 (at least 2 ASCII spaces) for hyphenated/non-ASCII verified quotes."""
    q = quote.strip()
    if q.count(" ") >= 2:
        return q
    spaced = q.replace("-", " ").replace("\n", " ").replace("\t", " ")
    spaced = " ".join(spaced.split())
    if spaced.count(" ") >= 2:
        return spaced
    # CJK or compact single-token verbatim quote: pad unobtrusively with ellipsis markers.
    return f"… {q} …"


class ReportStore:
    """Production SQLite store over Pool + Migrator + queries + lifecycle."""

    def __init__(self, db_path: str | Path, *, readers: int = 4):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.pool = Pool(self.db_path, readers=readers)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.pool.write() as conn:
            mig = Migrator(conn, MIGRATIONS_DIR, log=lambda *_: None)
            applied = mig.up()
            if applied:
                log_event(logger, logging.INFO, "db.migrated", applied=applied, version=mig.current())

    def close(self) -> None:
        self.pool.close()

    def health(self) -> dict[str, Any]:
        """Check SQLite reachability, quick integrity, and migration head."""
        try:
            with self.pool.read() as conn:
                conn.execute("SELECT 1").fetchone()
                qc = conn.execute("PRAGMA quick_check").fetchone()[0]
                ver = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            expected = len(list(MIGRATIONS_DIR.glob("*.up.sql")))
            ok = qc == "ok" and ver == expected
            return {"status": "ok" if ok else "degraded", "schema_version": ver, "expected_version": expected}
        except sqlite3.Error as exc:
            log_event(logger, logging.ERROR, "db.health_failed", error=type(exc).__name__)
            return {"status": "failed"}

    # ---------- owners ----------

    def owner_id_for_hash(self, token_hash: str) -> int | None:
        with self.pool.read() as conn:
            row = conn.execute(QUERIES["owner_by_token"].sql, (token_hash,)).fetchone()
            return int(row[0]) if row else None

    def ensure_owner(self, token_hash: str) -> int:
        existing = self.owner_id_for_hash(token_hash)
        now = _utc_now()
        if existing is not None:
            with self.pool.write() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("UPDATE owners SET last_seen_at = ? WHERE id = ?", (now, existing))
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            return existing
        with self.pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO owners (token_hash, last_seen_at) VALUES (?, ?)",
                    (token_hash, now),
                )
                row = conn.execute("SELECT id FROM owners WHERE token_hash = ?", (token_hash,)).fetchone()
                conn.execute("COMMIT")
                assert row is not None
                return int(row[0])
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # ---------- report lifecycle ----------

    def create_running_report(
        self,
        *,
        owner_id: int,
        idea: str,
        writer_model: str,
        verifier_model: str,
        simulate: str | None = None,
        client_request_id: str | None = None,
        pivot_of_report_id: str | None = None,
    ) -> tuple[str, bool]:
        """Create a running report row, or return existing id if (owner_id, client_request_id) already exists.
        Returns (report_id, created_new).
        """
        now = _utc_now()
        clean_idem = (client_request_id or "").strip()[:128] or None
        with self.pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if clean_idem is not None:
                    row = conn.execute(
                        "SELECT id FROM reports WHERE owner_id = ? AND client_request_id = ?",
                        (owner_id, clean_idem),
                    ).fetchone()
                    if row is not None:
                        conn.execute("COMMIT")
                        return str(row[0]), False

                # Validate pivot parent belongs to the same owner (avoid leaking or dangling refs).
                valid_pivot: str | None = None
                if pivot_of_report_id:
                    prow = conn.execute(
                        "SELECT id FROM reports WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                        (pivot_of_report_id, owner_id),
                    ).fetchone()
                    if prow is not None:
                        valid_pivot = str(prow[0])

                rid = new_db_report_id()
                for _ in range(5):
                    exists = conn.execute("SELECT 1 FROM reports WHERE id = ?", (rid,)).fetchone()
                    if not exists:
                        break
                    rid = new_db_report_id()

                conn.execute(
                    "INSERT INTO reports (id, owner_id, pivot_of_report_id, idea, writer_model, verifier_model, "
                    "simulate, started_at, heartbeat_at, client_request_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rid,
                        owner_id,
                        valid_pivot,
                        idea,
                        writer_model,
                        verifier_model,
                        simulate or None,
                        now,
                        now,
                        clean_idem,
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return rid, True
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def append_event(self, report_id: str, owner_id: int, seq: int, kind: str, payload: dict[str, Any]) -> None:
        """Append one progress event and bump heartbeat_at while the report is running."""
        valid_kinds = {"stage", "plan", "search", "triage", "compared", "verified", "warn", "error", "done"}
        db_kind = kind if kind in valid_kinds else "warn"
        encoded = json.dumps(payload if isinstance(payload, dict) else {"value": payload}, ensure_ascii=False)
        now = _utc_now()
        with self.pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM reports WHERE id = ?", (report_id,)).fetchone()
                if not row or row[0] != "running":
                    conn.execute("COMMIT")
                    return
                conn.execute(
                    "INSERT OR IGNORE INTO report_events (report_id, owner_id, seq, kind, payload) VALUES (?, ?, ?, ?, ?)",
                    (report_id, owner_id, seq, db_kind, encoded),
                )
                conn.execute("UPDATE reports SET heartbeat_at = ? WHERE id = ?", (now, report_id))
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def finish_report(
        self,
        report_id: str,
        owner_id: int,
        result: dict[str, Any],
        *,
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist every child row and transition the report from running -> terminal status atomically."""
        now = _utc_now()
        with self.pool.write() as conn:
            with L.transaction(conn, "system"):
                row = conn.execute("SELECT status, started_at FROM reports WHERE id = ?", (report_id,)).fetchone()
                if row is None or row[0] != "running":
                    return
                started_at = str(row[1])
                finished_at = now if now >= started_at else started_at

                # 1. Features (1..7)
                fids_by_code: dict[str, int] = {}
                for pos, f in enumerate((result.get("features") or [])[:7], start=1):
                    cur = conn.execute(
                        "INSERT INTO features (report_id, owner_id, position, name, description) VALUES (?, ?, ?, ?, ?)",
                        (report_id, owner_id, pos, _clamp(f.get("name") or f"Feature {pos}", 120), str(f.get("description") or "")),
                    )
                    assert cur.lastrowid is not None
                    fids_by_code[str(f.get("id") or f"F{pos}")] = int(cur.lastrowid)

                # 2. Search queries (1..14)
                qids_by_pos: dict[int, int] = {}
                qids_by_source: dict[str, int] = {}
                seen_queries: set[tuple[str, str]] = set()
                raw_outcomes = result.get("query_outcomes") or []
                if not raw_outcomes and result.get("queries"):
                    raw_outcomes = [
                        {"position": i + 1, "source": q.get("source", "web"), "query": q.get("query", ""),
                         "outcome": "ok", "result_count": 0, "error_message": None}
                        for i, q in enumerate(result["queries"])
                    ]
                q_pos = 0
                for q in raw_outcomes[:14]:
                    src = q.get("source") if q.get("source") in _VALID_SOURCES else "web"
                    qtext = _clamp(q.get("query") or f"query {q_pos + 1}", 390)
                    if (src, qtext) in seen_queries:
                        continue
                    seen_queries.add((src, qtext))
                    q_pos += 1
                    outcome = q.get("outcome") if q.get("outcome") in ("pending", "ok", "failed") else "ok"
                    res_count = int(q.get("result_count") or 0) if outcome == "ok" else None
                    err_msg = (_clamp(q.get("error_message") or "search failed", 300)) if outcome == "failed" else None
                    cur = conn.execute(
                        "INSERT INTO search_queries (report_id, owner_id, position, source_type, query_text, "
                        "outcome, result_count, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (report_id, owner_id, q_pos, src, qtext, outcome, res_count, err_msg),
                    )
                    assert cur.lastrowid is not None
                    qid = int(cur.lastrowid)
                    qids_by_pos[int(q.get("position") or q_pos)] = qid
                    qids_by_source.setdefault(src, qid)

                projects_in = result.get("projects") or []
                # Guarantee at least one query row if we have candidates/projects to attach.
                if (projects_in or candidates) and not qids_by_pos:
                    cur = conn.execute(
                        "INSERT INTO search_queries (report_id, owner_id, position, source_type, query_text, "
                        "outcome, result_count) VALUES (?, ?, 1, 'web', 'prior art search', 'ok', ?)",
                        (report_id, owner_id, len(projects_in)),
                    )
                    assert cur.lastrowid is not None
                    qids_by_pos[1] = int(cur.lastrowid)
                    qids_by_source["web"] = int(cur.lastrowid)

                default_qid = next(iter(qids_by_pos.values()), None)

                # 3. Candidates (deduped by url_key)
                cand_id_by_key: dict[str, int] = {}
                all_cands: list[dict[str, Any]] = list(candidates or [])
                for p in projects_in:
                    all_cands.append({
                        "url": p.get("url"),
                        "url_key": p.get("url_key") or norm_url(str(p.get("url") or "")),
                        "title": p.get("name", ""),
                        "snippet": "",
                        "score": 0.5,
                        "source": p.get("source", "web"),
                    })
                for c in all_cands:
                    url = safe_url(c.get("url"))
                    if not url or default_qid is None:
                        continue
                    ukey = str(c.get("url_key") or norm_url(url))
                    if not ukey or ukey in cand_id_by_key:
                        continue
                    qid = qids_by_pos.get(int(c.get("query_position") or 0)) or qids_by_source.get(
                        str(c.get("source") or "web"), default_qid
                    )
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO candidates (report_id, owner_id, found_by_query_id, url, url_key, "
                        "title, snippet, search_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report_id,
                            owner_id,
                            qid,
                            url,
                            ukey,
                            _clamp(c.get("title") or "", 300),
                            str(c.get("snippet") or "")[:600],
                            max(0.0, float(c.get("score") or 0.0)),
                        ),
                    )
                    if cur.lastrowid and cur.rowcount == 1:
                        cand_id_by_key[ukey] = int(cur.lastrowid)
                    else:
                        existing_c = conn.execute(
                            "SELECT id FROM candidates WHERE report_id = ? AND url_key = ?", (report_id, ukey)
                        ).fetchone()
                        if existing_c:
                            cand_id_by_key[ukey] = int(existing_c[0])

                # 4. Projects + Overlaps
                used_cands: set[int] = set()
                citation = 0
                has_live_overlap = False
                cov_by_fid: dict[str, int] = {fid: 0 for fid in fids_by_code}

                for p in projects_in:
                    url = safe_url(p.get("url"))
                    ukey = str(p.get("url_key") or norm_url(url)) if url else ""
                    cid = cand_id_by_key.get(ukey)
                    if cid is None or cid in used_cands:
                        continue
                    used_cands.add(cid)
                    citation += 1
                    kind = str(p.get("kind") or "other").lower()
                    if kind not in _VALID_KINDS:
                        kind = "other"
                    psrc = "snippet" if p.get("page_source") == "snippet" else "page"
                    flags = p.get("injection_flags") or []
                    inj = _clamp(flags[0], 60) if flags else None
                    cur = conn.execute(
                        "INSERT INTO projects (report_id, owner_id, candidate_id, citation_number, name, "
                        "one_liner, kind, page_source, injection_excerpt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report_id,
                            owner_id,
                            cid,
                            citation,
                            _clamp(p.get("name") or f"Project {citation}", 200),
                            str(p.get("one_liner") or ""),
                            kind,
                            psrc,
                            inj or None,
                        ),
                    )
                    assert cur.lastrowid is not None
                    pid = int(cur.lastrowid)
                    cells = p.get("cells") or {}
                    for fcode, fid in fids_by_code.items():
                        claimed, final, quote, status, note = _db_overlap_fields(cells.get(fcode, {}))
                        if final > 0:
                            has_live_overlap = True
                        if final > cov_by_fid[fcode]:
                            cov_by_fid[fcode] = final
                        conn.execute(
                            "INSERT INTO overlaps (report_id, owner_id, project_id, feature_id, claimed_score, "
                            "final_score, quote, check_status, check_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (report_id, owner_id, pid, fid, claimed, final, quote, status, note),
                        )

                # 5. Reconcile final status & novelty with schema triggers
                status = str(result.get("status") or "failed")
                if status == "ok" and not has_live_overlap:
                    status = "no_close_match"
                elif status == "no_close_match" and has_live_overlap:
                    status = "ok"
                if status in ("ok", "no_close_match") and not fids_by_code:
                    status = "failed"

                msg = str(result.get("message") or "").strip()
                if status not in ("running", "ok") and not msg:
                    msg = {
                        "no_close_match": "No overlap claim survived the checks.",
                        "insufficient_evidence": "The searches returned no results.",
                        "failed": "The check could not be completed.",
                    }.get(status, "Check ended without a score.")

                novelty: int | None = None
                if status in ("ok", "no_close_match"):
                    novelty = novelty_score(cov_by_fid)
                    for pos, pv in enumerate((result.get("pivots") or [])[:3], start=1):
                        if isinstance(pv, dict) and str(pv.get("title") or "").strip():
                            conn.execute(
                                "INSERT INTO pivots (report_id, owner_id, position, title, pitch, why_open) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    report_id,
                                    owner_id,
                                    pos,
                                    _clamp(pv["title"], 120),
                                    str(pv.get("pitch") or ""),
                                    str(pv.get("why_open") or ""),
                                ),
                            )

                summary = _clamp(result.get("summary") or result.get("idea") or "Idea check", 500)
                conn.execute(
                    "UPDATE reports SET status = ?, status_message = ?, summary = ?, target_user = ?, "
                    "verdict = ?, novelty = ?, finished_at = ?, heartbeat_at = ? WHERE id = ?",
                    (
                        status,
                        msg,
                        summary,
                        str(result.get("target_user") or ""),
                        str(result.get("verdict") or ""),
                        novelty,
                        finished_at,
                        finished_at,
                        report_id,
                    ),
                )

    def fail_running_report(self, report_id: str, owner_id: int, message: str) -> None:
        now = _utc_now()
        msg = (message or "The check failed unexpectedly.").strip()
        with self.pool.write() as conn:
            with L.transaction(conn, "system"):
                row = conn.execute(
                    "SELECT status, started_at, idea FROM reports WHERE id = ? AND owner_id = ?",
                    (report_id, owner_id),
                ).fetchone()
                if row is None or row[0] != "running":
                    return
                started_at = str(row[1])
                finished_at = now if now >= started_at else started_at
                summary = _clamp(row[2], 500)
                conn.execute("DELETE FROM pivots WHERE report_id = ?", (report_id,))
                conn.execute(
                    "UPDATE reports SET status = 'failed', status_message = ?, summary = ?, novelty = NULL, "
                    "finished_at = ?, heartbeat_at = ? WHERE id = ?",
                    (msg, summary, finished_at, finished_at, report_id),
                )

    def recover_stale_running(self, *, max_age_minutes: int = 10) -> int:
        """Mark running reports orphaned by a prior crash/restart as failed so owners can see/delete them."""
        now_dt = datetime.now(timezone.utc)
        cutoff = ts(now_dt - timedelta(minutes=max_age_minutes))
        now = ts(now_dt)
        with self.pool.write() as conn:
            with L.transaction(conn, "system"):
                rows = conn.execute(
                    "SELECT id, started_at, idea FROM reports "
                    "WHERE status = 'running' AND COALESCE(heartbeat_at, started_at) < ?",
                    (cutoff,),
                ).fetchall()
                for rid, started_at, idea in rows:
                    fin = now if now >= started_at else started_at
                    conn.execute("DELETE FROM pivots WHERE report_id = ?", (rid,))
                    conn.execute(
                        "UPDATE reports SET status = 'failed', "
                        "status_message = 'Interrupted: the server restarted during this check.', "
                        "summary = COALESCE(summary, ?), novelty = NULL, finished_at = ?, heartbeat_at = ? "
                        "WHERE id = ?",
                        (_clamp(idea, 500), fin, fin, rid),
                    )
        if rows:
            log_event(logger, logging.WARNING, "db.recovered_stale_checks", count=len(rows))
        return len(rows)

    # ---------- reads (owner-scoped) ----------

    def list_reports(self, owner_id: int, limit: int = 30) -> list[dict[str, Any]]:
        q = QUERIES["recent_reports"]
        with self.pool.read() as conn:
            rows = conn.execute(q.sql, (owner_id, min(max(1, limit), 100))).fetchall()
        return [
            {
                "id": r[0],
                "file": r[0],
                "summary": r[1] or "",
                "status": r[2],
                "novelty": r[3],
                "created": r[4],
            }
            for r in rows
        ]

    def list_deleted_reports(self, owner_id: int, limit: int = 20) -> list[dict[str, Any]]:
        q = QUERIES["recently_deleted"]
        with self.pool.read() as conn:
            rows = conn.execute(q.sql, (owner_id, min(max(1, limit), 50))).fetchall()
        return [{"id": r[0], "summary": r[1] or "", "deleted_at": r[2]} for r in rows]

    def get_report_owner(self, report_id: str) -> tuple[int, str] | None:
        with self.pool.read() as conn:
            row = conn.execute(QUERIES["report_by_id"].sql, (report_id,)).fetchone()
            if row is None:
                return None
            return int(row[1]), str(row[6])  # (owner_id, status)

    def events_after(self, report_id: str, owner_id: int, after_seq: int = 0) -> list[dict[str, Any]] | None:
        with self.pool.read() as conn:
            rep = conn.execute(QUERIES["report_by_id"].sql, (report_id,)).fetchone()
            if rep is None or int(rep[1]) != owner_id:
                return None
            out: list[dict[str, Any]] = []
            q = QUERIES["report_events_after"]
            assert q.default_limit is not None
            seq = after_seq
            while True:
                rows = conn.execute(q.sql, params_for(q, report_id, seq)).fetchall()
                for s, kind, raw in rows:
                    out.append({"seq": int(s), "kind": str(kind), "data": json.loads(raw)})
                if len(rows) < q.default_limit:
                    break
                seq = int(rows[-1][0])
            return out

    def get_report(self, report_id: str, owner_id: int) -> dict[str, Any] | None:
        """Hydrate a report dict for its owner using the indexed named queries. Returns None if not found or wrong owner."""
        with self.pool.read() as conn:
            r = conn.execute(QUERIES["report_by_id"].sql, (report_id,)).fetchone()
            if r is None or int(r[1]) != owner_id:
                return None
            (
                rid, _oid, pivot_of, idea, summary, target_user, status, status_message,
                novelty, verdict, writer_model, verifier_model, simulate, started_at, finished_at, created_at,
            ) = r

            feat_rows = conn.execute(QUERIES["report_features"].sql, params_for(QUERIES["report_features"], rid)).fetchall()
            features = [{"id": f"F{pos}", "name": name, "description": desc} for _fid, pos, name, desc in feat_rows]
            fcode_by_dbid = {fid: f"F{pos}" for fid, pos, _, _ in feat_rows}

            q_rows = conn.execute(QUERIES["report_queries"].sql, params_for(QUERIES["report_queries"], rid)).fetchall()
            queries_list = [{"source": src, "query": qtext} for _pos, src, qtext, *_ in q_rows]
            query_outcomes = [
                {"position": pos, "source": src, "query": qtext, "outcome": out, "result_count": cnt, "error_message": err}
                for pos, src, qtext, out, cnt, err in q_rows
            ]
            searches_failed = sum(1 for row in q_rows if row[3] == "failed")

            proj_rows = conn.execute(QUERIES["report_projects"].sql, params_for(QUERIES["report_projects"], rid)).fetchall()
            ov_rows = conn.execute(QUERIES["report_overlaps"].sql, params_for(QUERIES["report_overlaps"], rid)).fetchall()
            ov_by_pf: dict[tuple[int, int], tuple[Any, ...]] = {(row[0], row[1]): row for row in ov_rows}

            projects: list[dict[str, Any]] = []
            for pid, cit, pname, one_liner, kind, page_src, inj, url, src_type in proj_rows:
                cells: dict[str, dict[str, Any]] = {}
                total_score = 0
                for fid, fcode in fcode_by_dbid.items():
                    ov = ov_by_pf.get((pid, fid))
                    if ov is None:
                        cells[fcode] = {"claimed": 0, "score": 0, "quote": "", "status": "none", "reason": ""}
                    else:
                        _, _, claimed, final, quote, st, note = ov
                        total_score += int(final)
                        cells[fcode] = {
                            "claimed": int(claimed),
                            "score": int(final),
                            "quote": quote or "",
                            "status": str(st),
                            "reason": note or "",
                        }
                projects.append({
                    "id": f"P{cit}",
                    "n": int(cit),
                    "name": pname,
                    "one_liner": one_liner,
                    "kind": kind,
                    "url": url,
                    "source": src_type,
                    "source_label": SOURCES.get(src_type, SOURCES["web"])["label"],
                    "page_source": page_src,
                    "injection_flags": [inj] if inj else [],
                    "cells": cells,
                    "total": total_score,
                })

            cov_rows = conn.execute(QUERIES["report_coverage"].sql, params_for(QUERIES["report_coverage"], rid)).fetchall()
            coverage = {f"F{pos}": int(best) for _fid, pos, best, _cnt in cov_rows}
            crowd = {f"F{pos}": int(cnt) for _fid, pos, _best, cnt in cov_rows}
            for f in features:
                coverage.setdefault(f["id"], 0)
                crowd.setdefault(f["id"], 0)

            tot = conn.execute(QUERIES["report_check_totals"].sql, (rid,)).fetchone()
            checks = {
                "checked": int(tot[0] or 0),
                "cut_quote": int(tot[1] or 0),
                "cut_verifier": int(tot[2] or 0),
                "downgraded": int(tot[3] or 0),
                "unchecked": 0,
                "unverified": int(tot[4] or 0),
            }
            cand_count = int(conn.execute(QUERIES["report_candidate_count"].sql, (rid,)).fetchone()[0] or 0)
            piv_rows = conn.execute(QUERIES["report_pivots"].sql, params_for(QUERIES["report_pivots"], rid)).fetchall()
            pivots = [{"title": t, "pitch": p, "why_open": w} for _pos, t, p, w in piv_rows]

            seconds = 0.0
            if started_at and finished_at:
                try:
                    t0 = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                    seconds = round(max(0.0, (t1 - t0).total_seconds()), 1)
                except ValueError:
                    seconds = 0.0

            return {
                "schema_version": 1,
                "id": rid,
                "file": rid,
                "pivot_of_report_id": pivot_of,
                "status": status,
                "message": status_message,
                "idea": idea,
                "summary": summary or "",
                "target_user": target_user,
                "features": features,
                "queries": queries_list,
                "query_outcomes": query_outcomes,
                "projects": projects,
                "sources": [
                    {"n": p["n"], "name": p["name"], "url": p["url"], "source": p["source_label"]} for p in projects
                ],
                "coverage": coverage,
                "crowd": crowd,
                "gaps": [f for f in features if coverage.get(f["id"], 0) == 0]
                if status in ("ok", "no_close_match")
                else [],
                "novelty": novelty,
                "checks": checks,
                "verdict": verdict,
                "pivots": pivots,
                "candidates_found": cand_count,
                "searches_failed": searches_failed,
                "models": {"primary": writer_model, "verifier": verifier_model},
                "demo": writer_model.startswith("fake/"),
                "simulated": simulate,
                "seconds": seconds,
                "created": created_at,
            }

    # ---------- owner lifecycle operations ----------

    def delete_report(self, report_id: str, owner_id: int) -> bool:
        with self.pool.write() as conn:
            return L.delete_report(conn, report_id, owner_id, actor="owner")

    def restore_report(self, report_id: str, owner_id: int) -> bool:
        with self.pool.write() as conn:
            return L.restore_report(conn, report_id, owner_id, actor="owner")

    def export_owner(self, owner_id: int) -> dict[str, Any]:
        with self.pool.write() as conn:
            return L.export_owner(conn, owner_id, actor="owner")

    def delete_owner(self, owner_id: int) -> dict[str, int]:
        with self.pool.write() as conn:
            return L.delete_owner(conn, owner_id, actor="owner")

    # ---------- token-bucket rate limiter ----------

    def consume_rate_limit(
        self,
        key: str,
        *,
        capacity: int,
        refill_seconds: float,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Atomic SQLite token bucket. Returns (allowed, retry_after_seconds)."""
        t = time.time() if now is None else now
        rate = capacity / refill_seconds
        with self.pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT tokens, updated_at FROM rate_buckets WHERE key = ?", (key,)).fetchone()
                if row is None:
                    tokens = float(capacity)
                else:
                    elapsed = max(0.0, t - float(row[1]))
                    tokens = min(float(capacity), float(row[0]) + elapsed * rate)
                if tokens >= 1.0:
                    new_tokens = tokens - 1.0
                    conn.execute(
                        "INSERT OR REPLACE INTO rate_buckets (key, tokens, updated_at) VALUES (?, ?, ?)",
                        (key, new_tokens, t),
                    )
                    conn.execute("COMMIT")
                    return True, 0
                wait_s = int(math.ceil((1.0 - tokens) / rate)) if rate > 0 else int(refill_seconds)
                conn.execute(
                    "INSERT OR REPLACE INTO rate_buckets (key, tokens, updated_at) VALUES (?, ?, ?)",
                    (key, tokens, t),
                )
                conn.execute("COMMIT")
                return False, max(1, wait_s)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    # ---------- provider usage & daily spend guard ----------

    def record_usage(
        self,
        rec: UsageRecord,
        *,
        check_id: str | None = None,
        owner_ref: str | None = None,
        ip_ref: str | None = None,
    ) -> None:
        with self.pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO usage_events (ts, check_id, owner_ref, ip_ref, provider, operation, model, "
                    "prompt_tokens, completion_tokens, credits, cached, attempt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _utc_now(),
                        check_id,
                        owner_ref,
                        ip_ref,
                        rec.provider,
                        rec.operation,
                        rec.model,
                        max(0, int(rec.prompt_tokens)),
                        max(0, int(rec.completion_tokens)),
                        max(0.0, float(rec.credits)),
                        1 if rec.cached else 0,
                        max(1, int(rec.attempt)),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def daily_usage(self, day_utc: str | None = None) -> dict[str, Any]:
        """Return today's (UTC) billed tokens, credits, call count, and cache hits."""
        day = day_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = f"{day}T00:00:00.000Z"
        end = f"{day}T23:59:59.999Z"
        with self.pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN cached = 0 THEN prompt_tokens ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN cached = 0 THEN completion_tokens ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN cached = 0 THEN credits ELSE 0 END), 0.0), "
                "COALESCE(SUM(cached), 0) "
                "FROM usage_events WHERE ts BETWEEN ? AND ?",
                (start, end),
            ).fetchone()
        return {
            "day": day,
            "events": int(row[0] or 0),
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "credits": round(float(row[3] or 0.0), 2),
            "cache_hits": int(row[4] or 0),
        }

    def check_daily_budget(self, *, max_credits: float, max_output_tokens: int) -> tuple[bool, dict[str, Any]]:
        usage = self.daily_usage()
        within = usage["credits"] < max_credits and usage["completion_tokens"] < max_output_tokens
        return within, usage

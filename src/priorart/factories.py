"""Builders for every table, with sensible defaults and overrides. Used by the seed and by the tests.

Each builder inserts one row and returns its id. Pass `id=` (and `at=` for timestamps) to get the same rows
every time; leave them out and SQLite assigns ids and the current time.

    o = owner(conn, token="local-dev-token")
    r = report(conn, o, idea="A browser extension that ...")
    f1 = feature(conn, r, 1, "Summarize changes")
    ...
    finish(conn, r, "ok")                       # novelty is computed from the checked overlaps

`build_report` assembles a whole, consistent report in one call.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from .pipeline import norm_url

_BASE62 = string.digits + string.ascii_letters


def ts(at: datetime) -> str:
    """UTC ISO-8601 with milliseconds and 'Z': the only timestamp format the schema accepts."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_report_id() -> str:
    return "".join(secrets.choice(_BASE62) for _ in range(10))


def _stamps(at: datetime | None) -> dict:
    return {} if at is None else {"created_at": ts(at), "updated_at": ts(at)}


def _insert(conn, table: str, row: dict) -> Any:
    row = {k: v for k, v in row.items() if v is not _OMIT}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    return row.get("id", cur.lastrowid)


_OMIT = object()


def _owner_of(conn, report_id: str) -> int:
    row = conn.execute("SELECT owner_id FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise ValueError(f"no report {report_id!r}")
    return row[0]


# ---------- one builder per table -------------------------------------------------------------

def owner(conn, *, token: str | None = None, id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "owners", {"id": _OMIT if id is None else id,
                                    "token_hash": token_hash(token or secrets.token_hex(16)), **_stamps(at)})


def report(conn, owner_id: int, *, id: str | None = None,
           idea: str = "A browser extension that summarizes what changed in a site's terms of service.",
           writer_model: str = "meta-llama/Llama-3.3-70B-Instruct",
           verifier_model: str = "Qwen/Qwen3-235B-A22B-Instruct-2507",
           pivot_of: str | None = None, simulate: str | None = None, target_user: str = "",
           at: datetime | None = None) -> str:
    rid = id or new_report_id()
    _insert(conn, "reports", {"id": rid, "owner_id": owner_id, "idea": idea, "writer_model": writer_model,
                              "verifier_model": verifier_model, "pivot_of_report_id": pivot_of, "simulate": simulate,
                              "target_user": target_user,
                              **({} if at is None else {"started_at": ts(at)}), **_stamps(at)})
    return rid


def feature(conn, report_id: str, position: int, name: str | None = None, description: str = "", *,
            id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "features", {"id": _OMIT if id is None else id, "report_id": report_id,
                                      "owner_id": _owner_of(conn, report_id), "position": position,
                                      "name": name or f"Feature {position}", "description": description, **_stamps(at)})


def query(conn, report_id: str, position: int, source_type: str = "web", query_text: str | None = None, *,
          outcome: str = "ok", result_count: int | None = None, error_message: str | None = None,
          id: int | None = None, at: datetime | None = None) -> int:
    if outcome == "ok" and result_count is None:
        result_count = 5
    if outcome == "failed" and error_message is None:
        error_message = "search failed"
    return _insert(conn, "search_queries", {
        "id": _OMIT if id is None else id, "report_id": report_id, "owner_id": _owner_of(conn, report_id),
        "position": position, "source_type": source_type, "query_text": query_text or f"query {position}",
        "outcome": outcome, "result_count": result_count if outcome == "ok" else None,
        "error_message": error_message if outcome == "failed" else None, **_stamps(at)})


def candidate(conn, report_id: str, query_id: int, url: str, *, title: str = "", snippet: str = "",
              search_score: float = 0.5, id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "candidates", {
        "id": _OMIT if id is None else id, "report_id": report_id, "owner_id": _owner_of(conn, report_id),
        "found_by_query_id": query_id, "url": url, "url_key": norm_url(url), "title": title, "snippet": snippet[:600],
        "search_score": search_score, **_stamps(at)})


def project(conn, report_id: str, candidate_id: int, name: str = "Existing project", *,
            citation_number: int | None = None, one_liner: str = "", kind: str = "other", page_source: str = "page",
            injection_excerpt: str | None = None, id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "projects", {
        "id": _OMIT if id is None else id, "report_id": report_id, "owner_id": _owner_of(conn, report_id),
        "candidate_id": candidate_id, "citation_number": citation_number, "name": name, "one_liner": one_liner,
        "kind": kind, "page_source": page_source, "injection_excerpt": injection_excerpt, **_stamps(at)})


def overlap(conn, report_id: str, project_id: int, feature_id: int, claimed: int = 0, final: int | None = None, *,
            quote: str | None = None, status: str | None = None, note: str = "",
            id: int | None = None, at: datetime | None = None) -> int:
    """Defaults follow the schema's rules: no claim → status 'none'; a claim → 'verified' at the claimed score."""
    final = claimed if final is None else final
    if status is None:
        status = "none" if claimed == 0 else "verified"
    if claimed and quote is None:
        quote = "the project's page describes exactly this capability"
    return _insert(conn, "overlaps", {
        "id": _OMIT if id is None else id, "report_id": report_id, "owner_id": _owner_of(conn, report_id),
        "project_id": project_id, "feature_id": feature_id, "claimed_score": claimed, "final_score": final,
        "quote": quote if claimed else None, "check_status": status, "check_note": note, **_stamps(at)})


def pivot(conn, report_id: str, position: int, title: str | None = None, pitch: str = "", why_open: str = "", *,
          id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "pivots", {"id": _OMIT if id is None else id, "report_id": report_id,
                                    "owner_id": _owner_of(conn, report_id), "position": position,
                                    "title": title or f"Pivot {position}", "pitch": pitch, "why_open": why_open,
                                    **_stamps(at)})


def event(conn, report_id: str, seq: int, kind: str, payload: dict | None = None, *,
          id: int | None = None, at: datetime | None = None) -> int:
    return _insert(conn, "report_events", {"id": _OMIT if id is None else id, "report_id": report_id,
                                           "owner_id": _owner_of(conn, report_id), "seq": seq, "kind": kind,
                                           "payload": json.dumps(payload or {}, ensure_ascii=False), **_stamps(at)})


def novelty_for(conn, report_id: str) -> int:
    """The score the schema expects: 100 × (1 − checked coverage), from the feature_coverage view."""
    rows = conn.execute("SELECT best_score FROM feature_coverage WHERE report_id = ?", (report_id,)).fetchall()
    if not rows:
        raise ValueError(f"report {report_id} has no features")
    return round(100 * (1 - sum(r[0] for r in rows) / (2 * len(rows))))


def finish(conn, report_id: str, status: str = "ok", *, message: str | None = None, summary: str | None = None,
           verdict: str = "", novelty: int | None | str = "auto", at: datetime | None = None) -> None:
    """Move a running report to its final status. With `at`, the finish time and updated_at are exactly `at`."""
    if novelty == "auto":
        novelty = novelty_for(conn, report_id) if status in ("ok", "no_close_match") else None
    if message is None:
        message = "" if status in ("running", "ok") else {"no_close_match": "No overlap claim survived the checks.",
                   "insufficient_evidence": "The searches returned no results.",
                   "failed": "Every search failed."}[status]
    if summary is None:
        summary = conn.execute("SELECT substr(idea, 1, 200) FROM reports WHERE id = ?", (report_id,)).fetchone()[0]
    when = ts(at) if at is not None else None
    conn.execute(
        "UPDATE reports SET status = ?, status_message = ?, summary = ?, verdict = ?, novelty = ?, "
        "finished_at = COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "updated_at = COALESCE(?, updated_at) WHERE id = ?",
        (status, message, summary, verdict, novelty, when, when, report_id))


# ---------- a whole report in one call --------------------------------------------------------

def build_report(conn, owner_id: int, *, id: str | None = None, idea: str | None = None,
                 features: list | None = None, projects: list[dict] | None = None, pivots: list | None = None,
                 queries: list[dict] | None = None, events: list[tuple] | None = None,
                 status: str | None = "auto", message: str = "", verdict: str = "", summary: str | None = None,
                 pivot_of: str | None = None, target_user: str = "", simulate: str | None = None,
                 at: datetime | None = None, ids=None) -> str:
    """Build a consistent report and (unless status=None) finish it.

    features: names, or (name, description) pairs, in order (default: 3 features)
    projects: dicts with name, url, and optional source_type, kind, one_liner, page_source, injection_excerpt,
              title, snippet, and cells: {feature position: (claimed, final, status, quote, note)} — positions
              left out get no claim
    queries:  dicts with source_type, query_text, outcome, result_count, error_message (default: one per
              project source type, all 'ok')
    status:   'auto' → 'ok' if any checked overlap survives, else 'no_close_match'; None → leave it running
    ids:      an iterator of integer ids to use for every row (the seed passes one to fix ids)
    """
    def nid():
        return None if ids is None else next(ids)

    def when(sec: int):
        return None if at is None else at + timedelta(seconds=sec)

    rid = report(conn, owner_id, id=id, pivot_of=pivot_of, target_user=target_user, simulate=simulate, at=at,
                 **({} if idea is None else {"idea": idea}))
    feats = features if features is not None else ["Core capability", "Second capability", "Third capability"]
    fids = {}
    for pos, f in enumerate(feats, start=1):
        name, desc = (f, "") if isinstance(f, str) else f
        fids[pos] = feature(conn, rid, pos, name, desc, id=nid(), at=when(5))

    projects = projects or []
    if queries is None:
        sources = []
        for p in projects:
            s = p.get("source_type", "web")
            if s not in sources:
                sources.append(s)
        queries = [{"source_type": s, "query_text": f"{s} projects like this idea"} for s in (sources or ["web"])]
    qids: dict[str, int] = {}
    for pos, q in enumerate(queries, start=1):
        qid = query(conn, rid, pos, q.get("source_type", "web"), q.get("query_text"), outcome=q.get("outcome", "ok"),
                    result_count=q.get("result_count"), error_message=q.get("error_message"), id=nid(), at=when(10))
        qids.setdefault(q.get("source_type", "web"), qid)

    for n, p in enumerate(projects, start=1):
        src = p.get("source_type", "web")
        cid = candidate(conn, rid, qids.get(src) or next(iter(qids.values())), p["url"], title=p.get("title", p["name"]),
                        snippet=p.get("snippet", ""), search_score=p.get("search_score", round(0.9 - n * 0.05, 2)),
                        id=nid(), at=when(20))
        pid = project(conn, rid, cid, p["name"], citation_number=n, one_liner=p.get("one_liner", ""),
                      kind=p.get("kind", "other"), page_source=p.get("page_source", "page"),
                      injection_excerpt=p.get("injection_excerpt"), id=nid(), at=when(40))
        cells = p.get("cells", {})
        for pos, fid in fids.items():
            c = cells.get(pos)
            if c is None:
                overlap(conn, rid, pid, fid, 0, id=nid(), at=when(60))
            else:
                claimed, final, st, quote, note = (tuple(c) + (None, None, None, ""))[:5]
                overlap(conn, rid, pid, fid, claimed, final, status=st, quote=quote, note=note or "",
                        id=nid(), at=when(60))

    for pos, pv in enumerate(pivots or [], start=1):
        title, pitch, why = (pv, "", "") if isinstance(pv, str) else (tuple(pv) + ("", ""))[:3]
        pivot(conn, rid, pos, title, pitch, why, id=nid(), at=when(80))

    for seq, (kind, payload) in enumerate(events or [], start=1):
        event(conn, rid, seq, kind, payload, id=nid(), at=when(seq))

    if status is None:
        return rid
    if status == "auto":
        live = conn.execute("SELECT 1 FROM overlaps WHERE report_id = ? AND final_score > 0", (rid,)).fetchone()
        status = "ok" if live else "no_close_match"
    finish(conn, rid, status, message=message or None, verdict=verdict, summary=summary, at=when(95))
    return rid

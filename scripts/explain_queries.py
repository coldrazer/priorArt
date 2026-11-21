"""Capture query plans and index write costs against generated volume (docs/QUERY_PLAN.md).

  python scripts/explain_queries.py [--owners 1000] [--reports-per-owner 4]

Builds a throwaway in-memory database with every migration applied, fills it with seed.volume(), runs ANALYZE,
then prints:
  1. EXPLAIN QUERY PLAN for every query in priorart.queries (the 5 hottest first) and every foreign-key lookup,
     with the median time of each hot query over 200 runs against real ids;
  2. for every named index, what it costs: extra time to insert the table's rows with it vs. without, and its size.
Exits 1 if any plan scans a whole table.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.db import MIGRATIONS_DIR, connect  # noqa: E402
from priorart.migrate import Migrator  # noqa: E402
from priorart.queries import FK_LOOKUPS, HOT, QUERIES, params_for  # noqa: E402
from priorart.seed import volume  # noqa: E402


def sample_args(conn) -> dict[str, tuple]:
    """Real values from the generated data, so timings measure actual lookups."""
    rid, oid = conn.execute("SELECT id, owner_id FROM reports WHERE status = 'ok' ORDER BY id LIMIT 1 "
                            "OFFSET (SELECT COUNT(*) / 2 FROM reports WHERE status = 'ok')").fetchone()
    token_hash = conn.execute("SELECT token_hash FROM owners WHERE id = ?", (oid,)).fetchone()[0]
    url_key = conn.execute("SELECT url_key FROM candidates WHERE report_id = ? LIMIT 1", (rid,)).fetchone()[0]
    return {"owner_by_token": (token_hash,), "recent_reports": (oid,), "report_events_after": (rid, 5),
            "candidate_by_url_key": (rid, url_key), "pivots_of_report": (rid, oid), "recently_deleted": (oid,),
            "purge_due": ("2026-11-01T00:00:00.000Z",), "owner_reports_page": (oid, "", ""),
            "owner_audit_page": (oid, 0), "_default": (rid,)}


def plan(conn, sql: str, args: tuple) -> list[str]:
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]


def median_us(conn, sql: str, args: tuple, runs: int = 200) -> float:
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        conn.execute(sql, args).fetchall()
        samples.append((time.perf_counter() - t) * 1e6)
    return statistics.median(samples)


def index_costs(conn) -> list[tuple]:
    """Per named index: insert time for the table's rows without vs. with it (best of 3), and its size (dbstat)."""
    out = []
    rows = conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                        "ORDER BY tbl_name, name").fetchall()
    for name, table, sql in rows:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        best = {}
        for with_index in (False, True):
            runs = []
            for _ in range(3):
                conn.execute("DROP TABLE IF EXISTS bench")
                conn.execute(f"CREATE TABLE bench AS SELECT * FROM {table} WHERE 0")
                if with_index:
                    conn.execute(sql.replace(f"INDEX {name} ON {table}", "INDEX bench_ix ON bench", 1))
                t = time.perf_counter()
                conn.execute("BEGIN")
                conn.execute(f"INSERT INTO bench SELECT * FROM {table}")
                conn.execute("COMMIT")
                runs.append(time.perf_counter() - t)
            best[with_index] = min(runs)
        size_kb = conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = 'bench_ix'").fetchone()[0] / 1024
        conn.execute("DROP TABLE bench")
        extra_ms = (best[True] - best[False]) * 1000
        out.append((name, table, n, extra_ms, extra_ms / max(n, 1) * 10_000, size_kb))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="explain_queries.py")
    ap.add_argument("--owners", type=int, default=1000)
    ap.add_argument("--reports-per-owner", type=int, default=4)
    a = ap.parse_args(argv)

    conn = connect(":memory:")
    mig = Migrator(conn, MIGRATIONS_DIR, log=lambda *_: None)
    mig.up()
    t = time.perf_counter()
    counts = volume(conn, owners=a.owners, reports_per_owner=a.reports_per_owner)
    print(f"schema version {mig.current():04d}; generated in {time.perf_counter() - t:.1f}s: "
          + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    conn.execute("ANALYZE")
    args = sample_args(conn)

    scans = []
    print("\n== hot queries ==")
    for name in HOT + [n for n in QUERIES if n not in HOT]:
        q = QUERIES[name]
        bound = params_for(q, *args.get(name, args["_default"]))
        p = plan(conn, q.sql, bound)
        scans += [f"{name}: {s}" for s in p if s.startswith("SCAN")]
        timing = f"  median {median_us(conn, q.sql, bound):.0f} µs" if name in HOT else ""
        print(f"{name}{timing}")
        for line in p:
            print(f"    {line}")
    print("\n== foreign-key lookups ==")
    for name, sql in FK_LOOKUPS.items():
        p = plan(conn, sql, tuple([args["_default"][0]] * sql.count("?")))
        scans += [f"FK {name}: {s}" for s in p if s.startswith("SCAN")]
        print(f"{name}: {' | '.join(p)}")
    print("\n== index write cost (insert every row of the table without vs. with the index) ==")
    print(f"{'index':<40} {'table':<15} {'rows':>9} {'extra ms':>9} {'ms/10k':>7} {'size KB':>8}")
    for name, table, n, extra, per10k, kb in index_costs(conn):
        print(f"{name:<40} {table:<15} {n:>9,} {extra:>9.1f} {per10k:>7.2f} {kb:>8.0f}")
    print("\nfull-table scans:", "none" if not scans else "")
    for s in scans:
        print("  " + s)
    return 1 if scans else 0


if __name__ == "__main__":
    sys.exit(main())

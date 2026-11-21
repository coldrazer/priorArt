"""Print daily provider spend, token usage, cache hit rate, and budget utilization.

  python scripts/usage.py [--db PATH] [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.config import Settings  # noqa: E402
from priorart.db import MIGRATIONS_DIR, connect, db_path  # noqa: E402
from priorart.migrate import Migrator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="usage.py", description="Prior Art daily spend report")
    ap.add_argument("--db", help="database file (default: $PRIORART_DB_PATH or data/priorart.sqlite)")
    ap.add_argument("--day", help="UTC date YYYY-MM-DD (default: today)")
    a = ap.parse_args(argv)

    settings = Settings.from_env(validate=False)
    path = Path(a.db) if a.db else db_path()
    if not path.exists():
        print(f"No database found at {path}.", file=sys.stderr)
        return 1

    conn = connect(path)
    try:
        Migrator(conn, MIGRATIONS_DIR, log=lambda *_: None).verify()
        day = a.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start, end = f"{day}T00:00:00.000Z", f"{day}T23:59:59.999Z"
        tot = conn.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN cached = 0 THEN prompt_tokens ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN cached = 0 THEN completion_tokens ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN cached = 0 THEN credits ELSE 0 END), 0.0), "
            "COALESCE(SUM(cached), 0) "
            "FROM usage_events WHERE ts BETWEEN ? AND ?",
            (start, end),
        ).fetchone()
        events, ptok, ctok, credits, hits = int(tot[0]), int(tot[1]), int(tot[2]), float(tot[3]), int(tot[4])
        hit_rate = (100.0 * hits / events) if events else 0.0
        print(f"Prior Art usage — {day} (UTC)")
        print(f"  Total calls:    {events:,} (cache hits: {hits:,}, {hit_rate:.1f}%)")
        print(f"  Prompt tokens:  {ptok:,}")
        print(f"  Output tokens:  {ctok:,} / {settings.daily_budget_output_tokens:,}")
        print(f"  Tavily credits: {credits:.1f} / {settings.daily_budget_credits}")

        rows = conn.execute(
            "SELECT provider, model, operation, COUNT(*), SUM(cached), "
            "SUM(CASE WHEN cached = 0 THEN completion_tokens ELSE 0 END), "
            "SUM(CASE WHEN cached = 0 THEN credits ELSE 0 END) "
            "FROM usage_events WHERE ts BETWEEN ? AND ? "
            "GROUP BY provider, model, operation ORDER BY provider, operation",
            (start, end),
        ).fetchall()
        if rows:
            print("\nBy provider & operation:")
            for prov, model, op, n, ch, out_tok, cr in rows:
                label = f"{prov}/{op}" + (f" ({model.split('/')[-1]})" if model else "")
                print(f"  {label:<44} calls={n:<4} cached={ch:<4} out_tok={int(out_tok):<8} credits={float(cr):.1f}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

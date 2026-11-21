"""Database migrations for local development and CI.

  python scripts/db.py status                 # what's applied, what's pending
  python scripts/db.py up [--to N]            # apply pending migrations (all, or up to version N)
  python scripts/db.py down [--steps N | --to N]   # roll back (default: the last one)
  python scripts/db.py reset --yes            # local only: roll everything back, then apply everything
  python scripts/db.py check                  # CI: up, down to zero, up again, compare with the model

Database file: --db PATH, else $PRIORART_DB_PATH, else data/priorart.sqlite.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.db import MIGRATIONS_DIR, MODEL_SCHEMA, connect, db_path  # noqa: E402
from priorart.migrate import DEFAULT_LARGE_TABLE_ROWS, MigrationError, Migrator, check  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="db.py", description="Prior Art database migrations")
    ap.add_argument("--db", help="database file (default: $PRIORART_DB_PATH or data/priorart.sqlite)")
    ap.add_argument("--large-table-rows", type=int,
                    default=int(os.getenv("PRIORART_LARGE_TABLE_ROWS", DEFAULT_LARGE_TABLE_ROWS)),
                    help="row count above which table-locking statements are refused")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    up = sub.add_parser("up")
    up.add_argument("--to", type=int)
    down = sub.add_parser("down")
    g = down.add_mutually_exclusive_group()
    g.add_argument("--to", type=int)
    g.add_argument("--steps", type=int)
    reset = sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true", help="confirm: this deletes all data in the database")
    sub.add_parser("check")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "check":
            problems = check(MIGRATIONS_DIR, MODEL_SCHEMA, connect)
            for p in problems:
                print(f"FAIL {p}")
            print("check passed: up → down → up is clean and matches src/priorart/schema.sql" if not problems
                  else f"check failed: {len(problems)} problem(s)")
            return 1 if problems else 0

        path = Path(a.db) if a.db else db_path()
        if a.cmd == "reset":
            if os.getenv("PRIORART_ENV", "development") == "production":
                print("reset refused: PRIORART_ENV is production", file=sys.stderr)
                return 2
            if not a.yes:
                print(f"reset deletes every row in {path}. Re-run with --yes to confirm.", file=sys.stderr)
                return 2

        conn = connect(path)
        mig = Migrator(conn, MIGRATIONS_DIR, large_table_rows=a.large_table_rows)
        if a.cmd == "status":
            rows = mig.status()
            print(f"database: {path}")
            for r in rows:
                state = f"applied {r['applied_at']}" if r["applied_at"] else "pending"
                rev = "" if r["reversible"] else "  (irreversible)"
                print(f"  {r['label']:<40} {state}{rev}")
            print(f"current version: {mig.current():04d} of {len(rows):04d}")
        elif a.cmd == "up":
            done = mig.up(to=a.to)
            print(f"up to {mig.current():04d}" + ("" if done else " (nothing to apply)"))
        elif a.cmd == "down":
            done = mig.down(to=a.to, steps=a.steps)
            print(f"down to {mig.current():04d}" + ("" if done else " (nothing to roll back)"))
        elif a.cmd == "reset":
            mig.down(to=0)
            mig.up()
            print(f"reset {path}: now at {mig.current():04d}")
        return 0
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

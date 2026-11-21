"""Load the seeded owners (see README "Seeded data"). Safe to run any number of times.

  python scripts/seed.py [--db PATH]

Refuses when PRIORART_ENV=production (no override), and when the database has migrations still to apply.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.db import MIGRATIONS_DIR, connect, db_path  # noqa: E402
from priorart.migrate import MigrationError, Migrator  # noqa: E402
from priorart.seed import SEED_OWNERS, seed  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seed.py", description="Load deterministic seed data")
    ap.add_argument("--db", help="database file (default: $PRIORART_DB_PATH or data/priorart.sqlite)")
    a = ap.parse_args(argv)

    if os.getenv("PRIORART_ENV", "development") == "production":
        print("seed refused: PRIORART_ENV is production. Seed data never goes into a production database.",
              file=sys.stderr)
        return 2
    path = Path(a.db) if a.db else db_path()
    conn = connect(path)
    try:
        mig = Migrator(conn, MIGRATIONS_DIR, log=lambda *_: None)
        pending = [m.label for m in mig.migrations if m.version > mig.current()]
    except MigrationError as e:
        print(f"seed refused: {e}", file=sys.stderr)
        return 1
    if pending:
        print(f"seed refused: migrations not applied yet ({', '.join(pending)}). Run: python scripts/db.py up",
              file=sys.stderr)
        return 1

    t0 = time.perf_counter()
    result = seed(conn)
    took = time.perf_counter() - t0
    print(f"seeded {path} in {took:.2f}s")
    for name, _, oid in SEED_OWNERS:
        n = conn.execute("SELECT COUNT(*) FROM reports WHERE owner_id = ?", (oid,)).fetchone()[0]
        print(f"  owner {name:<8} id {oid}  reports {n}")
    print("  rows: " + ", ".join(f"{t} {n}" for t, n in result["counts"].items()))
    print(f"  fingerprint {result['fingerprint']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

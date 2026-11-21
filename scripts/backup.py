"""Backups for the Prior Art database (docs/DATA_LIFECYCLE.md §4).

  python scripts/backup.py backup [--keep 48]        # online backup into data/backups/, verified; keep the newest N
  python scripts/backup.py list                      # backups with size, schema version and integrity
  python scripts/backup.py restore FILE --yes        # replace the database with a backup (stop the app first)
  python scripts/backup.py drill [--owners N]        # timed restore drill on a throwaway copy

Database: --db PATH, else $PRIORART_DB_PATH, else data/priorart.sqlite. Backups: --dir, else data/backups/.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.db import MIGRATIONS_DIR, connect, db_path  # noqa: E402
from priorart.lifecycle import LifecycleError, backup, database_fingerprint, restore, verify  # noqa: E402
from priorart.migrate import Migrator  # noqa: E402
from priorart.seed import seed, volume  # noqa: E402


def _remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def cmd_backup(db: Path, folder: Path, keep: int) -> int:
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 1
    folder.mkdir(parents=True, exist_ok=True)
    name = f"priorart-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite"
    info = backup(db, folder / name)
    print(f"backup {info['path']}  {info['bytes'] / 1e6:.1f} MB  schema {info['schema_version']:04d}  "
          f"integrity {info['integrity']}  {info['seconds']:.2f}s")
    old = sorted(folder.glob("priorart-*.sqlite"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink()
    if old:
        print(f"removed {len(old)} older backup(s), keeping {keep}")
    return 0


def cmd_list(folder: Path) -> int:
    files = sorted(folder.glob("priorart-*.sqlite"))
    if not files:
        print(f"no backups in {folder}")
    for f in files:
        try:
            v = verify(f)
            state = f"schema {v['schema_version']:04d}  integrity {v['integrity']}"
        except Exception as e:  # any failure to open or check the file means the backup is unusable
            state = f"UNUSABLE: {e}"
        print(f"{f.name}  {f.stat().st_size / 1e6:.1f} MB  {state}")
    return 0


def cmd_restore(db: Path, file: Path, yes: bool) -> int:
    if not yes:
        print(f"restore replaces everything in {db} with {file}. Stop the app, then re-run with --yes.",
              file=sys.stderr)
        return 2
    info = restore(file, db)
    print(f"restored {db} from {file} in {info['seconds']:.2f}s: schema {info['schema_version']:04d}, "
          f"integrity {info['integrity']}")
    return 0


def cmd_drill(owners: int, reports_per_owner: int) -> int:
    """Build a realistic database, back it up, lose it, restore it, and prove the data is identical. Timed."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "drill.sqlite"
        conn = connect(db)
        Migrator(conn, MIGRATIONS_DIR, log=lambda *_: None).up()
        seed(conn)
        volume(conn, owners=owners, reports_per_owner=reports_per_owner)
        conn.close()
        before = database_fingerprint(db)
        size = db.stat().st_size + Path(str(db) + "-wal").stat().st_size if Path(str(db) + "-wal").exists() else db.stat().st_size
        print(f"1. database built: {size / 1e6:.1f} MB, fingerprint {before[:16]}…")
        b = backup(db, Path(tmp) / "backup.sqlite")
        print(f"2. backup taken and verified: {b['bytes'] / 1e6:.1f} MB in {b['seconds']:.2f}s")
        _remove_db(db)
        print(f"3. database deleted (simulated loss): exists={db.exists()}")
        t = time.perf_counter()
        r = restore(Path(tmp) / "backup.sqlite", db)
        after = database_fingerprint(db)
        total = time.perf_counter() - t
        print(f"4. restored: copy {r['seconds']:.2f}s, integrity {r['integrity']}, schema {r['schema_version']:04d}")
        print(f"5. fingerprint after restore {after[:16]}… {'MATCHES' if after == before else 'DIFFERS'}")
        print(f"restore time (restore + verify + fingerprint): {total:.2f}s")
        return 0 if after == before else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backup.py")
    ap.add_argument("--db")
    ap.add_argument("--dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--keep", type=int, default=48)
    sub.add_parser("list")
    r = sub.add_parser("restore")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    d = sub.add_parser("drill")
    d.add_argument("--owners", type=int, default=1000)
    d.add_argument("--reports-per-owner", type=int, default=4)
    a = ap.parse_args(argv)

    db = Path(a.db) if a.db else db_path()
    folder = Path(a.dir) if a.dir else Path(os.getenv("PRIORART_BACKUP_DIR", str(db.parent / "backups")))
    try:
        if a.cmd == "backup":
            return cmd_backup(db, folder, a.keep)
        if a.cmd == "list":
            return cmd_list(folder)
        if a.cmd == "restore":
            return cmd_restore(db, Path(a.file), a.yes)
        return cmd_drill(a.owners, a.reports_per_owner)
    except LifecycleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Data rights and retention (docs/DATA_LIFECYCLE.md §3).

  python scripts/data.py export --owner-token TOKEN [--out FILE]   # everything stored about an owner, as JSON
  python scripts/data.py delete-owner --owner-token TOKEN --yes    # permanently delete an owner and their data
  python scripts/data.py purge                                     # remove reports deleted > 30 days ago

--owner-id N can replace --owner-token. Database: --db PATH, else $PRIORART_DB_PATH, else data/priorart.sqlite.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from priorart.db import connect, db_path  # noqa: E402
from priorart.lifecycle import LifecycleError, delete_owner, export_owner, owner_id_for_token, purge_expired  # noqa: E402


def _owner(conn, a) -> int:
    if a.owner_id is not None:
        return a.owner_id
    oid = owner_id_for_token(conn, a.owner_token)
    if oid is None:
        raise LifecycleError("no owner with that token")
    return oid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="data.py")
    ap.add_argument("--db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "delete-owner"):
        p = sub.add_parser(name)
        who = p.add_mutually_exclusive_group(required=True)
        who.add_argument("--owner-token")
        who.add_argument("--owner-id", type=int)
        if name == "export":
            p.add_argument("--out", help="write to this file instead of standard output")
        else:
            p.add_argument("--yes", action="store_true", help="confirm: this cannot be undone")
    sub.add_parser("purge")
    a = ap.parse_args(argv)

    conn = connect(Path(a.db) if a.db else db_path())
    try:
        if a.cmd == "export":
            data = export_owner(conn, _owner(conn, a), actor="operator")
            text = json.dumps(data, indent=2, ensure_ascii=False)
            if a.out:
                Path(a.out).write_text(text)
                print(f"exported {len(data['reports'])} report(s) and {len(data['audit'])} audit entr(ies) to {a.out}")
            else:
                print(text)
        elif a.cmd == "delete-owner":
            oid = _owner(conn, a)
            if not a.yes:
                print(f"this permanently deletes owner {oid} and all their reports. Export first if they want a "
                      "copy, then re-run with --yes.", file=sys.stderr)
                return 2
            counts = delete_owner(conn, oid, actor="operator")
            print(f"deleted owner {oid}: " + ", ".join(f"{t} {n}" for t, n in counts.items()))
        else:
            print(f"purged {purge_expired(conn)} report(s) deleted more than 30 days ago")
        return 0
    except LifecycleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

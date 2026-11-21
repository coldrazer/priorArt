"""Versioned, reversible SQLite migrations. Rules for authors: docs/MIGRATIONS.md.

A migration is a set of files in migrations/ sharing a 4-digit version and a snake_case name:

    0002_add_report_title.up.sql            required: applies the change
    0002_add_report_title.down.sql          reverses it, or instead ...
    0002_add_report_title.irreversible.md   ... a written recovery plan (sections "## Why" and "## Recovery")

Guarantees:
- versions apply in numeric order and must run 1..N with no gaps or duplicates
- each migration runs in one transaction together with its history row: all of it or none of it
- foreign keys are checked before commit; a migration that leaves a dangling reference is rolled back
- an applied migration whose file changed (checksum mismatch) blocks every command until resolved
- statements that lock or rewrite a large table are refused unless the migration says why it's safe
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

FILE_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.(?P<kind>up\.sql|down\.sql|irreversible\.md)$")
ALLOW_BLOCKING_RE = re.compile(r"^--[ \t]*priorart:[ \t]*allow-blocking[ \t]+(?P<reason>\S[^\n]*)$", re.MULTILINE)
DEFAULT_LARGE_TABLE_ROWS = 100_000

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY CHECK (version >= 1),
  name        TEXT NOT NULL CHECK (length(name) > 0),
  checksum    TEXT NOT NULL CHECK (length(checksum) = 64),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT
"""

# Statements a migration file may not contain: the runner owns transactions and connection settings.
FORBIDDEN = [
    (re.compile(r"^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I),
     "transaction control (the runner wraps each migration in its own transaction)"),
    (re.compile(r"^\s*PRAGMA\b", re.I), "PRAGMA (connection settings belong in priorart.db.connect)"),
    (re.compile(r"^\s*(VACUUM|ATTACH|DETACH)\b", re.I), "VACUUM/ATTACH/DETACH (can't run inside a migration transaction)"),
]

# Statements that lock or rewrite a whole table. (label, regex capturing the affected table)
_IDENT = r'["`\[]?(?P<table>\w+)["`\]]?'
BLOCKING = [
    ("CREATE INDEX", re.compile(rf"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b.*?\bON\s+{_IDENT}", re.I | re.S)),
    ("ALTER TABLE ... DROP COLUMN (rewrites the table)", re.compile(rf"^\s*ALTER\s+TABLE\s+{_IDENT}\s+DROP\b", re.I | re.S)),
    ("UPDATE without WHERE (rewrites every row)", re.compile(rf"^\s*UPDATE\s+{_IDENT}\s+SET\b(?!.*\bWHERE\b)", re.I | re.S)),
    ("DELETE without WHERE", re.compile(rf"^\s*DELETE\s+FROM\s+{_IDENT}(?!.*\bWHERE\b)", re.I | re.S)),
    ("table copy (INSERT ... SELECT reads the whole table)", re.compile(rf"^\s*INSERT\s+INTO\b.*?\bSELECT\b.*?\bFROM\s+{_IDENT}", re.I | re.S)),
]

# Matches single-quoted string literals (preserved), -- line comments (stripped), or /* ... */ block comments (stripped).
_SQL_TOKEN_RE = re.compile(r"'(?:''|[^'])*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Path
    down: Path | None
    recovery: Path | None

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"

    @property
    def reversible(self) -> bool:
        return self.down is not None

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up.read_bytes()).hexdigest()


# ---------- reading migration files -----------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (trigger bodies with inner semicolons stay whole)."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if _strip_comments(stmt).strip(" ;\n\t"):
                out.append(stmt)
            buf = ""
    if _strip_comments(buf).strip(" ;\n\t"):
        raise MigrationError(f"incomplete SQL statement at end of file: {buf.strip()[:80]!r}")
    return out


def _strip_comments(sql: str) -> str:
    """Strip both -- line comments and /* ... */ block comments while preserving string literals."""
    def _repl(m: re.Match[str]) -> str:
        tok = m.group(0)
        return tok if tok.startswith("'") else " "

    return _SQL_TOKEN_RE.sub(_repl, sql)


def discover(directory: Path) -> list[Migration]:
    files: dict[int, dict[str, Any]] = {}
    for path in sorted(Path(directory).iterdir()):
        if path.name.startswith(".") or path.name == "README.md":
            continue
        m = FILE_RE.match(path.name)
        if not m:
            raise MigrationError(
                f"{path.name}: not a migration file name (expected NNNN_snake_name.up.sql / "
                ".down.sql / .irreversible.md)"
            )
        v = int(m["version"])
        entry = files.setdefault(v, {"name": m["name"]})
        if entry["name"] != m["name"]:
            raise MigrationError(f"version {v:04d} is used by two names: {entry['name']} and {m['name']}")
        entry[m["kind"]] = path
    migrations = []
    for v in sorted(files):
        e = files[v]
        label = f"{v:04d}_{e['name']}"
        if "up.sql" not in e:
            raise MigrationError(f"{label}: missing {label}.up.sql")
        if ("down.sql" in e) == ("irreversible.md" in e):
            raise MigrationError(f"{label}: needs exactly one of {label}.down.sql or {label}.irreversible.md")
        if "irreversible.md" in e:
            text = e["irreversible.md"].read_text()
            for heading in ("## Why", "## Recovery"):
                body = text.split(heading, 1)[1].split("\n## ", 1)[0].strip() if heading in text else ""
                if not body:
                    raise MigrationError(f"{label}.irreversible.md needs a non-empty '{heading}' section")
        migrations.append(Migration(v, e["name"], e["up.sql"], e.get("down.sql"), e.get("irreversible.md")))
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise MigrationError(
            f"versions must run 0001..{len(migrations):04d} with no gaps; found "
            + ", ".join(m.label for m in migrations)
        )
    for mig in migrations:
        for path in filter(None, (mig.up, mig.down)):
            lint(path)
    return migrations


def lint(path: Path) -> None:
    for stmt in split_statements(path.read_text()):
        body = _strip_comments(stmt).strip()
        for rx, why in FORBIDDEN:
            if rx.match(body):
                raise MigrationError(f"{path.name}: {why} is not allowed: {body[:60]!r}")


# ---------- running them ----------------------------------------------------------------------

class Migrator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        directory: Path,
        large_table_rows: int = DEFAULT_LARGE_TABLE_ROWS,
        log: Callable[[str], Any] = print,
    ):
        if conn.isolation_level is not None:
            raise MigrationError("connect with isolation_level=None (use priorart.db.connect)")
        self.conn = conn
        self.dir = Path(directory)
        self.large_table_rows = large_table_rows
        self.log = log
        self.migrations = discover(self.dir)
        conn.execute(HISTORY_DDL)

    # state
    def applied(self) -> dict[int, tuple[str, str, str]]:
        rows = self.conn.execute("SELECT version, name, checksum, created_at FROM schema_migrations ORDER BY version")
        return {v: (n, c, t) for v, n, c, t in rows}

    def verify(self) -> None:
        """Refuse to do anything if history and files disagree."""
        applied = self.applied()
        by_version = {m.version: m for m in self.migrations}
        if list(applied) != list(range(1, len(applied) + 1)):
            raise MigrationError(f"history has gaps: applied versions {list(applied)}")
        for v, (name, checksum, _) in applied.items():
            m = by_version.get(v)
            if m is None:
                raise MigrationError(f"version {v:04d}_{name} is applied but its files are missing from {self.dir}")
            if m.name != name:
                raise MigrationError(f"version {v:04d} was applied as '{name}' but the file is now '{m.name}'")
            if m.checksum != checksum:
                raise MigrationError(
                    f"{m.label}.up.sql changed after it was applied. Never edit an applied "
                    "migration: restore the file and write a new migration instead."
                )

    def status(self) -> list[dict[str, Any]]:
        self.verify()
        applied = self.applied()
        return [
            {
                "version": m.version,
                "label": m.label,
                "applied_at": applied[m.version][2] if m.version in applied else None,
                "reversible": m.reversible,
            }
            for m in self.migrations
        ]

    def current(self) -> int:
        return max(self.applied(), default=0)

    # moving
    def up(self, to: int | None = None) -> list[str]:
        self.verify()
        target = len(self.migrations) if to is None else to
        if not 0 <= target <= len(self.migrations):
            raise MigrationError(f"no version {target:04d}; latest is {len(self.migrations):04d}")
        done = []
        for m in self.migrations:
            if self.current() < m.version <= target:
                self._run(m, m.up, "up")
                done.append(m.label)
        return done

    def down(self, to: int | None = None, steps: int | None = None) -> list[str]:
        self.verify()
        cur = self.current()
        if to is None:
            to = max(0, cur - (1 if steps is None else steps))
        if not 0 <= to <= cur:
            raise MigrationError(f"can't roll down to {to:04d} from {cur:04d}")
        going = [m for m in reversed(self.migrations) if to < m.version <= cur]
        blocked = [m for m in going if not m.reversible]
        if blocked:
            m = blocked[0]
            raise MigrationError(f"{m.label} is irreversible; nothing was rolled back. Recovery plan: {m.recovery}")
        done = []
        for m in going:
            assert m.down is not None
            self._run(m, m.down, "down")
            done.append(m.label)
        return done

    def _run(self, m: Migration, path: Path, direction: str) -> None:
        sql = path.read_text()
        statements = split_statements(sql)
        self._guard_blocking(path, sql, statements)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                self.conn.execute(stmt)
            problems = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise MigrationError(
                    f"{path.name} leaves {len(problems)} broken foreign key reference(s), "
                    f"e.g. table {problems[0][0]} row {problems[0][1]} → {problems[0][2]}"
                )
            if direction == "up":
                self.conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                    (m.version, m.name, m.checksum),
                )
            else:
                self.conn.execute("DELETE FROM schema_migrations WHERE version = ?", (m.version,))
            self.conn.execute("COMMIT")
        except Exception as e:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            if isinstance(e, MigrationError):
                raise
            raise MigrationError(f"{path.name} failed and was rolled back: {e}") from e
        self.log(f"{'applied' if direction == 'up' else 'reverted'} {m.label}")

    def _guard_blocking(self, path: Path, sql: str, statements: list[str]) -> None:
        allow = ALLOW_BLOCKING_RE.search(sql)
        for stmt in statements:
            body = _strip_comments(stmt).strip()
            for label, rx in BLOCKING:
                hit = rx.match(body)
                if not hit:
                    continue
                table = hit["table"]
                exists = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue  # table is new or gone: nothing to lock
                rows = self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if rows > self.large_table_rows and not allow:
                    raise MigrationError(
                        f"{path.name}: {label} on '{table}' ({rows:,} rows, limit {self.large_table_rows:,}) "
                        "locks the database while it runs. Split it per docs/MIGRATIONS.md, or add "
                        "'-- priorart: allow-blocking <why this is safe>' to the file."
                    )


# ---------- drift ------------------------------------------------------------------------------

def schema_snapshot(conn: sqlite3.Connection) -> dict[str, str]:
    """Every user object (table, index, view, trigger) → its SQL, normalized so formatting and comments don't count."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations' AND sql IS NOT NULL"
    )
    return {f"{t} {n}": normalize_sql(s) for t, n, s in rows}


def normalize_sql(sql: str) -> str:
    s = _strip_comments(sql).lower()
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*([(),;=<>])\s*", r"\1", s)
    return s


def diff_snapshots(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    out = []
    for key in sorted(set(expected) | set(actual)):
        if key not in actual:
            out.append(f"missing from migrations: {key}")
        elif key not in expected:
            out.append(f"not in the model (schema.sql): {key}")
        elif expected[key] != actual[key]:
            out.append(f"differs: {key}")
    return out


def check(directory: Path, model_schema: Path, connect_fn: Callable[[str], sqlite3.Connection], log: Callable[[str], Any] = print) -> list[str]:
    """CI check: empty → up → down to zero → up again, then compare with the model. Returns problems ([] = pass)."""
    problems: list[str] = []
    conn = connect_fn(":memory:")
    mig = Migrator(conn, directory, log=log)
    log(f"migrations found: {len(mig.migrations)}")
    mig.up()
    first = schema_snapshot(conn)
    log(f"up: {len(first)} objects")
    mig.down(to=0)
    left = schema_snapshot(conn)
    if left:
        problems.append("down to 0 left objects behind: " + ", ".join(sorted(left)))
    if mig.applied():
        problems.append("down to 0 left rows in schema_migrations")
    log(f"down: {len(left)} objects left")
    try:
        mig.up()
    except MigrationError as e:
        problems.append(f"re-apply failed: {e}")
        return problems
    second = schema_snapshot(conn)
    log(f"up again: {len(second)} objects")
    problems += [f"re-apply differs: {d}" for d in diff_snapshots(first, second)]

    model = connect_fn(":memory:")
    model.executescript(model_schema.read_text())
    problems += [f"drift: {d}" for d in diff_snapshots(schema_snapshot(model), second)]
    return problems

"""The migration runner: ordering, history, rollback, irreversible migrations, lock guard, drift."""
import subprocess
import sys
from pathlib import Path

import pytest

from priorart.db import MIGRATIONS_DIR, MODEL_SCHEMA, connect
from priorart.migrate import MigrationError, Migrator, check, discover, split_statements

ROOT = Path(__file__).resolve().parents[1]
QUIET = dict(log=lambda *_: None)


def write(d: Path, files: dict[str, str]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


BASIC = {
    "0001_items.up.sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL) STRICT;",
    "0001_items.down.sql": "DROP TABLE items;",
    "0002_item_n_index.up.sql": "CREATE INDEX items_n ON items (n);",
    "0002_item_n_index.down.sql": "DROP INDEX items_n;",
}


def mig(tmp_path, files=BASIC, **kw):
    return Migrator(connect(":memory:"), write(tmp_path / "m", files), **QUIET, **kw)


def tables(m):
    return {r[0] for r in m.conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


# ---------- discovery and ordering

def test_applies_in_numeric_order_and_records_history(tmp_path):
    files = {**BASIC, "0010_x.up.sql": "SELECT 1;", "0010_x.down.sql": "SELECT 1;"}
    files.update({f"{i:04d}_s{i}.up.sql": "SELECT 1;" for i in range(3, 10)})
    files.update({f"{i:04d}_s{i}.down.sql": "SELECT 1;" for i in range(3, 10)})
    m = mig(tmp_path, files)
    assert [x.version for x in m.migrations] == list(range(1, 11))
    assert m.up() == [x.label for x in m.migrations]
    assert list(m.applied()) == list(range(1, 11))
    assert m.up() == []  # idempotent


@pytest.mark.parametrize("files,msg", [
    ({"0001_a.up.sql": "SELECT 1;", "0001_a.down.sql": "SELECT 1;",
      "0003_c.up.sql": "SELECT 1;", "0003_c.down.sql": "SELECT 1;"}, "no gaps"),
    ({"0001_a.up.sql": "SELECT 1;", "0001_a.down.sql": "SELECT 1;", "0001_b.up.sql": "SELECT 1;"}, "two names"),
    ({"0001_a.up.sql": "SELECT 1;"}, "exactly one of"),
    ({"0001_a.up.sql": "SELECT 1;", "0001_a.down.sql": "SELECT 1;", "0001_a.irreversible.md": "x"}, "exactly one of"),
    ({"0001_a.down.sql": "SELECT 1;"}, "missing"),
    ({"1_a.up.sql": "SELECT 1;"}, "not a migration file name"),
    ({"0001_Bad-Name.up.sql": "SELECT 1;"}, "not a migration file name"),
    ({"0001_a.up.sql": "SELECT 1;", "0001_a.irreversible.md": "## Why\nbecause\n"}, "Recovery"),
    ({"0001_a.up.sql": "BEGIN;\nCREATE TABLE t (x INTEGER);\nCOMMIT;", "0001_a.down.sql": "DROP TABLE t;"}, "transaction control"),
    ({"0001_a.up.sql": "PRAGMA foreign_keys = OFF;", "0001_a.down.sql": "SELECT 1;"}, "PRAGMA"),
    ({"0001_a.up.sql": "CREATE TABLE t (x INTEGER", "0001_a.down.sql": "SELECT 1;"}, "incomplete"),
])
def test_bad_migration_sets_are_refused(tmp_path, files, msg):
    with pytest.raises(MigrationError, match=msg):
        discover(write(tmp_path / "m", files))


def test_split_keeps_trigger_bodies_whole():
    sql = ("CREATE TABLE t (x INTEGER);\n-- note; with semicolon\nCREATE TRIGGER tr AFTER INSERT ON t BEGIN\n"
           "  UPDATE t SET x = 1 WHERE rowid = NEW.rowid;\n  SELECT 1;\nEND;\n")
    parts = split_statements(sql)
    assert len(parts) == 2 and parts[1].endswith("END;")


# ---------- up / down / history integrity

def test_down_reverses_one_step_by_default_then_to_zero(tmp_path):
    m = mig(tmp_path)
    m.up()
    assert m.down() == ["0002_item_n_index"] and "items_n" not in tables(m)
    m.down(to=0)
    assert tables(m) == {"schema_migrations"} and m.applied() == {}


def test_up_to_a_version(tmp_path):
    m = mig(tmp_path)
    assert m.up(to=1) == ["0001_items"] and m.current() == 1


def test_failed_migration_rolls_back_completely(tmp_path):
    files = {**BASIC, "0003_broken.up.sql": "CREATE TABLE half (x INTEGER);\nINSERT INTO nowhere VALUES (1);",
             "0003_broken.down.sql": "DROP TABLE half;"}
    m = mig(tmp_path, files)
    with pytest.raises(MigrationError, match="rolled back"):
        m.up()
    assert m.current() == 2 and "half" not in tables(m)


def test_dangling_foreign_key_rolls_back(tmp_path):
    files = {"0001_p.up.sql": "CREATE TABLE p (id INTEGER PRIMARY KEY);\n"
                              "CREATE TABLE c (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p (id));",
             "0001_p.down.sql": "DROP TABLE c;\nDROP TABLE p;",
             "0002_bad.up.sql": "CREATE TABLE c2 (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p (id) DEFERRABLE INITIALLY DEFERRED);\n"
                                "INSERT INTO c2 (p_id) VALUES (42);",
             "0002_bad.down.sql": "DROP TABLE c2;"}
    m = mig(tmp_path, files)
    with pytest.raises(MigrationError, match="foreign key"):
        m.up()
    assert m.current() == 1 and "c2" not in tables(m)


def test_edited_applied_migration_blocks_everything(tmp_path):
    m = mig(tmp_path)
    m.up()
    (m.dir / "0001_items.up.sql").write_text("CREATE TABLE items (id INTEGER PRIMARY KEY) STRICT;")
    m2 = Migrator(m.conn, m.dir, **QUIET)
    for action in (m2.up, m2.down, m2.status):
        with pytest.raises(MigrationError, match="changed after it was applied"):
            action()


def test_applied_migration_missing_from_disk(tmp_path):
    m = mig(tmp_path)
    m.up()
    for f in ("0002_item_n_index.up.sql", "0002_item_n_index.down.sql"):
        (m.dir / f).unlink()
    with pytest.raises(MigrationError, match="files are missing"):
        Migrator(m.conn, m.dir, **QUIET).status()


def test_irreversible_migration_blocks_rollback_past_it(tmp_path):
    files = {**BASIC, "0003_drop_n.up.sql": "ALTER TABLE items DROP COLUMN n;",
             "0003_drop_n.irreversible.md": "## Why\nThe values are discarded.\n\n## Recovery\nRestore from backup.\n"}
    files["0002_item_n_index.up.sql"] = "SELECT 1;"
    files["0002_item_n_index.down.sql"] = "SELECT 1;"
    m = mig(tmp_path, files)
    m.up()
    assert m.status()[2]["reversible"] is False
    with pytest.raises(MigrationError, match="irreversible"):
        m.down(to=1)
    assert m.current() == 3  # nothing rolled back


# ---------- lock guard

def _big(tmp_path, extra, limit=10):
    """items gets 50 rows in 0002, above the guard's limit of 10."""
    files = {**BASIC, **extra}
    files["0002_item_n_index.up.sql"] = ("WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 50) "
                                         "INSERT INTO items (n) SELECT i FROM s;")
    files["0002_item_n_index.down.sql"] = "DELETE FROM items WHERE id > 0;"
    return mig(tmp_path, files, large_table_rows=limit)


@pytest.mark.parametrize("sql,what", [
    ("CREATE INDEX items_n2 ON items (n);", "CREATE INDEX"),
    ("ALTER TABLE items DROP COLUMN n;", "DROP COLUMN"),
    ("UPDATE items SET n = n + 1;", "UPDATE without WHERE"),
    ("DELETE FROM items;", "DELETE without WHERE"),
    ("CREATE TABLE items2 (id INTEGER, n INTEGER);\nINSERT INTO items2 SELECT id, n FROM items;", "table copy"),
])
def test_blocking_statements_on_large_tables_are_refused(tmp_path, sql, what):
    m = _big(tmp_path, {"0003_risky.up.sql": sql, "0003_risky.down.sql": "SELECT 1;"})
    m.up(to=2)
    with pytest.raises(MigrationError, match=what):
        m.up()
    assert m.current() == 2


def test_allow_blocking_needs_a_reason_and_then_passes(tmp_path):
    m = _big(tmp_path, {"0003_risky.up.sql": "-- priorart: allow-blocking\nCREATE INDEX items_n2 ON items (n);",
                        "0003_risky.down.sql": "DROP INDEX items_n2;"})
    m.up(to=2)
    with pytest.raises(MigrationError):
        m.up()
    m2 = _big(tmp_path / "b", {"0003_risky.up.sql": "-- priorart: allow-blocking runs before launch, table is tiny in prod\n"
                                                   "CREATE INDEX items_n2 ON items (n);",
                               "0003_risky.down.sql": "DROP INDEX items_n2;"})
    assert m2.up()[-1] == "0003_risky"


def test_small_tables_and_new_tables_are_not_blocked(tmp_path):
    m = mig(tmp_path, large_table_rows=10)
    assert len(m.up()) == 2


def test_update_with_where_is_allowed_on_large_tables(tmp_path):
    m = _big(tmp_path, {"0003_batch.up.sql": "UPDATE items SET n = n + 1 WHERE id BETWEEN 1 AND 5;",
                        "0003_batch.down.sql": "UPDATE items SET n = n - 1 WHERE id BETWEEN 1 AND 5;"})
    assert m.up()[-1] == "0003_batch"


# ---------- drift check and the real migrations

def test_real_migrations_pass_the_ci_check():
    assert check(MIGRATIONS_DIR, MODEL_SCHEMA, connect, **QUIET) == []


def test_drift_from_model_is_reported(tmp_path):
    model = tmp_path / "schema.sql"
    model.write_text("CREATE TABLE items (id INTEGER PRIMARY KEY, n INTEGER NOT NULL, extra TEXT) STRICT;\n"
                     "CREATE INDEX items_n ON items (n);")
    problems = check(write(tmp_path / "m", BASIC), model, connect, **QUIET)
    assert problems == ["drift: differs: table items"]


def test_down_that_leaves_objects_behind_is_reported(tmp_path):
    files = {**BASIC, "0002_item_n_index.up.sql": "CREATE TABLE extra (x INTEGER) STRICT;",
             "0002_item_n_index.down.sql": "SELECT 1;"}
    model = tmp_path / "schema.sql"
    model.write_text(BASIC["0001_items.up.sql"] + "\nCREATE TABLE extra (x INTEGER) STRICT;")
    problems = check(write(tmp_path / "m", files), model, connect, **QUIET)
    assert any("left objects behind: table extra" in p for p in problems)
    assert any("re-apply failed" in p for p in problems)


def test_cli_status_up_down(tmp_path):
    db = tmp_path / "x.sqlite"
    run = lambda *a: subprocess.run([sys.executable, "scripts/db.py", "--db", str(db), *a], cwd=ROOT,
                                    capture_output=True, text=True)
    assert "pending" in run("status").stdout
    assert run("up").returncode == 0 and "applied" in run("status").stdout
    assert run("down").returncode == 0 and "pending" in run("status").stdout
    r = run("reset")
    assert r.returncode == 2 and "--yes" in r.stderr
    assert run("reset", "--yes").returncode == 0

"""Every application query and foreign-key lookup uses an index (03.3, docs/QUERY_PLAN.md)."""
import re

import pytest

from priorart.db import MIGRATIONS_DIR, connect
from priorart.migrate import Migrator
from priorart.queries import FK_LOOKUPS, HOT, QUERIES, TRIGGER_LOOKUPS, params_for
from priorart.seed import volume


@pytest.fixture(scope="module")
def conn():
    c = connect(":memory:")
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    volume(c, owners=60, reports_per_owner=3)
    c.execute("ANALYZE")
    return c


def _plan(c, sql, args):
    return [row[3] for row in c.execute("EXPLAIN QUERY PLAN " + sql, args)]


@pytest.mark.parametrize("name", list(QUERIES))
def test_query_uses_an_index_never_a_full_scan(conn, name):
    q = QUERIES[name]
    plan = _plan(conn, q.sql, params_for(q, *([1] * (q.sql.count("?") - (q.default_limit is not None)))))
    assert not [p for p in plan if p.startswith("SCAN")], plan
    assert any(re.search(r"USING (COVERING )?INDEX|PRIMARY KEY", p) for p in plan), plan


@pytest.mark.parametrize("name", list(FK_LOOKUPS))
def test_every_foreign_key_lookup_is_indexed(conn, name):
    sql = FK_LOOKUPS[name]
    plan = _plan(conn, sql, tuple([1] * sql.count("?")))
    assert plan and all(p.startswith("SEARCH") for p in plan), plan


def _table_scans(c, sql):
    """Plan lines that scan a table. 'SCAN <view>' only walks a view's own few result rows, so it's allowed;
    any other SCAN (plans name tables by alias, e.g. 'SCAN f') reads a whole table."""
    views = {v for (v,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    return [p for p in _plan(c, sql, tuple(["x"] * sql.count("?"))) if p.startswith("SCAN") and p.split()[1] not in views]


@pytest.mark.parametrize("name", list(TRIGGER_LOOKUPS))
def test_finish_trigger_lookups_are_indexed(conn, name):
    assert _table_scans(conn, TRIGGER_LOOKUPS[name]) == []


def test_trigger_check_catches_the_pre_0003_view():
    c = connect(":memory:")
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up(to=2)
    assert _table_scans(c, TRIGGER_LOOKUPS["coverage sum"])   # the old view scanned every feature


def test_every_list_query_has_a_limit():
    for q in QUERIES.values():
        returns_many = q.default_limit is not None
        has_limit = bool(re.search(r"\bLIMIT \?$", q.sql))
        assert returns_many == has_limit, q.name
        if not returns_many:  # single-row by construction: equality on a unique key, or an aggregate
            assert re.search(r"WHERE (token_hash|id) = \?( AND deleted_at IS NULL)?$|^SELECT COUNT"
                             r"|WHERE report_id = \? AND url_key = \?$", q.sql), q.name


def test_every_reports_query_respects_soft_delete():
    for q in QUERIES.values():
        if re.search(r"\bFROM reports\b", q.sql):
            hides_deleted = "deleted_at IS NULL" in q.sql
            assert hides_deleted != q.includes_deleted, q.name


def test_hot_queries_return_real_rows(conn):
    rid, oid = conn.execute("SELECT id, owner_id FROM reports WHERE status = 'ok' LIMIT 1").fetchone()
    assert conn.execute(QUERIES["report_by_id"].sql, (rid,)).fetchone()[0] == rid
    recent = conn.execute(QUERIES["recent_reports"].sql, params_for(QUERIES["recent_reports"], oid)).fetchall()
    assert 1 <= len(recent) <= QUERIES["recent_reports"].default_limit
    assert [r[4] for r in recent] == sorted((r[4] for r in recent), reverse=True)
    events = conn.execute(QUERIES["report_events_after"].sql, (rid, 5, 10)).fetchall()
    assert [e[0] for e in events] == list(range(6, 16))
    cells = conn.execute(QUERIES["report_overlaps"].sql, params_for(QUERIES["report_overlaps"], rid)).fetchall()
    assert len(cells) == 30   # 6 projects × 5 features
    assert set(HOT) <= set(QUERIES)


def _overlapping_indexes(c):
    """Plain (non-unique, non-partial) indexes whose leading column already leads another index on the table.

    Such an index serves the same lookups as the other one, so it only adds write cost. Unique and partial
    indexes encode rules, so they are kept as the ones that serve.
    """
    found = []
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        idx = []
        for _, name, unique, _origin, partial in c.execute(f"PRAGMA index_list('{table}')"):
            cols = [r[2] for r in c.execute(f"PRAGMA index_info('{name}')")]
            idx.append((name, cols, bool(unique), bool(partial)))
        for name, cols, unique, partial in idx:
            if unique or partial:
                continue
            for other, ocols, _, opartial in idx:
                if other != name and not opartial and ocols[0] == cols[0]:
                    found.append(f"{name}{cols} overlaps {other}{ocols}")
    return found


def test_no_overlapping_indexes(conn):
    assert _overlapping_indexes(conn) == []


def test_overlap_check_catches_the_0001_index_set():
    c = connect(":memory:")
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up(to=1)
    names = {f.split("[")[0] for f in _overlapping_indexes(c)}
    assert names == {"features_report_owner", "search_queries_report_owner", "candidates_report_owner",
                     "projects_report_owner", "pivots_report_owner", "report_events_report_owner",
                     "projects_candidate", "overlaps_project"}

"""Seed data and factories (03.4)."""
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from priorart import factories as F
from priorart.db import MIGRATIONS_DIR, connect
from priorart.migrate import Migrator
from priorart.seed import seed

ROOT = Path(__file__).resolve().parents[1]


def fresh():
    c = connect(":memory:")
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    return c


def test_seed_is_idempotent_and_deterministic():
    c = fresh()
    first = seed(c)
    second = seed(c)
    assert first == second
    other = fresh()
    assert seed(other)["fingerprint"] == first["fingerprint"]  # same result in a different database


def test_seed_leaves_other_owners_alone():
    c = fresh()
    o = F.owner(c, token="someone-else")
    r = F.build_report(c, o)
    before = c.execute("SELECT * FROM reports WHERE id = ?", (r,)).fetchone()
    seed(c)
    seed(c)
    assert c.execute("SELECT * FROM reports WHERE id = ?", (r,)).fetchone() == before


def test_seed_covers_every_state():
    c = fresh()
    seed(c)
    statuses = {s for (s,) in c.execute("SELECT DISTINCT status FROM reports WHERE owner_id = 900002")}
    assert statuses == {"ok", "no_close_match", "insufficient_evidence", "failed", "running"}
    outcomes = {s for (s,) in c.execute("SELECT DISTINCT check_status FROM overlaps WHERE owner_id IN (900002, 900003)")}
    assert outcomes == {"none", "verified", "downgraded", "unverified", "cut_quote", "cut_verifier"}
    assert c.execute("SELECT COUNT(*) FROM reports WHERE owner_id = 900001").fetchone() == (0,)
    caps = c.execute("SELECT length(idea) FROM reports WHERE id = 'SdHvyCaps1'").fetchone()[0]
    assert caps == 4000
    assert c.execute("SELECT COUNT(*) FROM features WHERE report_id = 'SdHvyCaps1'").fetchone() == (7,)
    assert c.execute("SELECT COUNT(*) FROM search_queries WHERE report_id = 'SdHvyCaps1'").fetchone() == (14,)
    assert c.execute("SELECT COUNT(*) FROM pivots WHERE report_id = 'SdHvyCaps1'").fetchone() == (3,)
    assert c.execute("SELECT COUNT(*) FROM projects WHERE injection_excerpt IS NOT NULL").fetchone() == (1,)
    assert c.execute("SELECT COUNT(*) FROM projects WHERE page_source = 'snippet'").fetchone() == (1,)
    assert c.execute("SELECT pivot_of_report_id FROM reports WHERE id = 'SdTypPvt01'").fetchone() == ("SdTypOk001",)
    assert c.execute("SELECT COUNT(*) FROM reports WHERE owner_id = 900003").fetchone() == (121,)
    assert c.execute("SELECT COUNT(*) FROM reports WHERE idea GLOB '*多言語*' AND idea GLOB '*ملاحظات*'").fetchone() == (1,)


def test_factories_defaults_and_overrides():
    c = fresh()
    at = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
    o = F.owner(c, token="tok", id=7, at=at)
    assert c.execute("SELECT token_hash, created_at FROM owners WHERE id = 7").fetchone() == (F.token_hash("tok"),
                                                                                            "2026-01-02T03:04:05.678Z")
    r = F.build_report(c, o, projects=[{"name": "X", "url": "https://example.com/x",
                                        "cells": {1: (2,), 2: (2, 1, "downgraded", "it does a weaker version of this", "partial")}}])
    novelty, status = c.execute("SELECT novelty, status FROM reports WHERE id = ?", (r,)).fetchone()
    assert status == "ok" and novelty == F.novelty_for(c, r) == 50   # coverage 2 + 1 of 6
    empty = F.build_report(c, o, projects=[])
    assert c.execute("SELECT status, novelty FROM reports WHERE id = ?", (empty,)).fetchone() == ("no_close_match", 100)
    running = F.build_report(c, o, status=None)
    assert c.execute("SELECT status FROM reports WHERE id = ?", (running,)).fetchone() == ("running",)


def test_cli_seeds_twice_with_same_fingerprint(tmp_path):
    db = str(tmp_path / "s.sqlite")
    env = {**os.environ, "PRIORART_ENV": "development"}
    subprocess.run([sys.executable, "scripts/db.py", "--db", db, "up"], cwd=ROOT, env=env, check=True, capture_output=True)
    outs = [subprocess.run([sys.executable, "scripts/seed.py", "--db", db], cwd=ROOT, env=env, check=True,
                           capture_output=True, text=True).stdout for _ in range(2)]
    fp = [line.split()[-1] for out in outs for line in out.splitlines() if "fingerprint" in line]
    assert len(fp) == 2 and fp[0] == fp[1]


def test_cli_refuses_production_and_unmigrated(tmp_path):
    db = str(tmp_path / "p.sqlite")
    prod = subprocess.run([sys.executable, "scripts/seed.py", "--db", db], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "PRIORART_ENV": "production"})
    assert prod.returncode == 2 and "production" in prod.stderr
    assert not Path(db).exists()   # refused before touching the file
    raw = subprocess.run([sys.executable, "scripts/seed.py", "--db", db], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PRIORART_ENV": "development"})
    assert raw.returncode == 1 and "db.py up" in raw.stderr

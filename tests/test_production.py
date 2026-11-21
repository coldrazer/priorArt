"""Production-grade verification suite: evidence integrity, security, resilience, DB wiring, cost caps, UI invariants."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from priorart import ConfigError, Settings, build
from priorart.__main__ import main as cli_main
from priorart.cache import Cache
from priorart.db import MIGRATIONS_DIR, Pool, connect
from priorart.fakes import FakeLLM, FakeSearch
from priorart.lifecycle import backup, restore
from priorart.llm import LLMError, parse_json
from priorart.log import JsonFormatter, capture_error, redact
from priorart.migrate import MigrationError, Migrator, lint
from priorart.pipeline import (
    Clients,
    PriorArt,
    clean_idea,
    norm_url,
    parse_bool,
    parse_score,
    quote_in_page,
)
from priorart.queries import QUERIES, params_for
from priorart.report import md_escape, to_markdown
from priorart.resilience import BudgetExceeded, CheckBudget, CircuitBreaker, ProviderUnavailable, retry_call
from priorart.search import Search, safe_url
from priorart.server import INDEX_HTML, compute_csp_header, create_app
from priorart.store import ReportStore

# ---------- 1. Evidence integrity (S.10 #01-#05, N1-N7) ----------

def test_verifier_skipped_claim_is_unchecked_never_verified(tmp_path):
    class SkippingVerifier(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            out = super().json(task, system, payload, role, max_tokens)
            if task == "verify" and payload["project"] == "NoveltyCheck":
                out["results"] = [r for r in out["results"] if r["key"] != "F1"]
            return out

    s = Settings(fake=True, runs_dir=str(tmp_path))
    r = PriorArt(Clients(SkippingVerifier(), FakeSearch()), s).run("A research agent that checks if an idea exists")
    nc = next(p for p in r["projects"] if p["name"] == "NoveltyCheck")
    assert nc["cells"]["F1"]["status"] == "unchecked"
    assert r["checks"]["unchecked"] == 1
    md = to_markdown(r)
    assert "not independently checked" in md


@pytest.mark.parametrize("raw,expected", [
    (True, True), ("true", True), ("YES", True), (1, True),
    (False, False), ("false", False), ("No", False), (0, False),
    ("maybe", None), (None, None), (2, None), ([], None),
])
def test_parse_bool_strict(raw, expected):
    assert parse_bool(raw) is expected


@pytest.mark.parametrize("raw,hi,expected", [
    (2, 2, 2), ("2", 2, 2), (2.0, 2, 2), (5, 2, 2), (-1, 2, 0),
    (1, 1, 1), ("1.0", 2, 1), ("x", 2, None), (None, 2, None), (True, 2, None), (float("nan"), 2, None),
])
def test_parse_score_strict(raw, hi, expected):
    assert parse_score(raw, hi=hi) == expected


def test_verify_one_table_driven_including_string_false_and_downgrade(tmp_path):
    class CustomVerifier(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            if task == "verify":
                return {"results": [
                    {"key": "F1", "supported": "false", "adjusted_score": 0, "reason": "string false must cut"},
                    {"key": "F2", "supported": False, "adjusted_score": 1, "reason": "only partial"},
                    {"key": "F3", "supported": "maybe", "adjusted_score": 2, "reason": "ambiguous"},
                    {"key": "F4", "supported": True, "adjusted_score": 2, "reason": "clearly shown"},
                ]}
            return super().json(task, system, payload, role, max_tokens)

    s = Settings(fake=True, runs_dir=str(tmp_path))
    agent = PriorArt(Clients(CustomVerifier(), FakeSearch()), s)
    plan = {"features": [{"id": f"F{i}", "name": f"Feat {i}", "description": "desc"} for i in range(1, 5)]}
    page = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    proj = {
        "name": "Demo",
        "page": page,
        "cells": {
            "F1": {"claimed": 2, "score": 2, "quote": "alpha beta gamma", "status": "claimed", "reason": ""},
            "F2": {"claimed": 2, "score": 2, "quote": "delta epsilon zeta", "status": "claimed", "reason": ""},
            "F3": {"claimed": 2, "score": 2, "quote": "eta theta iota", "status": "claimed", "reason": ""},
            "F4": {"claimed": 2, "score": 2, "quote": "kappa lambda mu", "status": "claimed", "reason": ""},
        },
    }
    stats = agent.verify_one(plan, proj)
    assert proj["cells"]["F1"]["status"] == "cut_verifier" and proj["cells"]["F1"]["score"] == 0
    assert proj["cells"]["F2"]["status"] == "downgraded" and proj["cells"]["F2"]["score"] == 1
    assert proj["cells"]["F3"]["status"] == "unchecked" and proj["cells"]["F3"]["score"] == 2
    assert proj["cells"]["F4"]["status"] == "verified" and proj["cells"]["F4"]["score"] == 2
    assert stats == {"checked": 4, "cut_quote": 0, "cut_verifier": 1, "downgraded": 1, "unchecked": 1}


def test_quote_in_page_unicode_and_negation_safety():
    # Unicode scripts (N4)
    assert quote_in_page("家族全員のメモをリアルタイムで翻訳します", "多言語ノートは家族全員のメモをリアルタイムで翻訳します。")
    assert quote_in_page("دعم النص من اليمين إلى اليسار", "تطبيق دعم النص من اليمين إلى اليسار للغة العربية")
    # Middle-word minor diff still matches
    assert quote_in_page(
        "DeepDig searches GitHub Reddit Product Hunt and arXiv daily",
        "DeepDig searches GitHub Hacker News Product Hunt and arXiv daily",
    )
    # Negation inversion MUST be rejected
    assert not quote_in_page(
        "fully support live voice transcription",
        "This tool does NOT support live voice transcription at all.",
    )


def test_norm_url_preserves_meaningful_query_and_strips_tracking():
    assert norm_url("https://news.ycombinator.com/item?id=111") != norm_url("https://news.ycombinator.com/item?id=222")
    assert norm_url("https://www.youtube.com/watch?v=abc&utm_source=hn&ref=home") == norm_url("https://youtube.com/watch?v=abc")
    assert norm_url("https://example.com/p?b=2&a=1#frag") == norm_url("http://www.example.com/p/?a=1&b=2")


def test_concurrent_saves_never_overwrite_and_omit_saved_to_on_disk(tmp_path):
    s = Settings(fake=True, runs_dir=str(tmp_path))
    agent = build(s)
    base = {"summary": "Same idea", "status": "ok", "id": "AbCdEf1234"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        paths = list(ex.map(lambda _: agent.save(dict(base)), range(50)))
    files = list(tmp_path.glob("*.json"))
    assert len(set(paths)) == 50 and len(files) == 50
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "saved_to" not in data and data["file"] == f.name


def test_verifier_outage_preserves_check_totals_and_markdown_warnings(tmp_path):
    class VerifierDown(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            if task == "verify":
                raise RuntimeError("verifier 503")
            return super().json(task, system, payload, role, max_tokens)

    s = Settings(fake=True, runs_dir=str(tmp_path))
    r = PriorArt(Clients(VerifierDown(), FakeSearch()), s).run("A research agent that checks if an idea exists")
    actual_claimed = sum(1 for p in r["projects"] for c in p["cells"].values() if c["claimed"] > 0)
    assert r["checks"]["checked"] == actual_claimed
    assert r["checks"]["cut_quote"] == 1
    assert r["checks"]["unverified"] == actual_claimed - 1
    md = to_markdown(r)
    assert "verifier model failed" in md
    assert "unverified" in md.lower()


def test_triage_failure_falls_back_and_empty_triage_gives_no_close_match(tmp_path):
    class TriageCrash(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            if task == "triage":
                raise RuntimeError("triage timeout")
            return super().json(task, system, payload, role, max_tokens)

    warns: list[str] = []
    s = Settings(fake=True, runs_dir=str(tmp_path))
    r = PriorArt(Clients(TriageCrash(), FakeSearch()), s, emit=lambda k, d: warns.append(d["message"]) if k == "warn" else None).run(
        "A research agent that checks if an idea exists"
    )
    assert r["status"] in ("ok", "no_close_match")
    assert any("shortlist" in w.lower() for w in warns)

    class TriageEmpty(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            if task == "triage":
                return {"picked": []}
            return super().json(task, system, payload, role, max_tokens)

    r2 = PriorArt(Clients(TriageEmpty(), FakeSearch()), s).run("A research agent that checks if an idea exists")
    assert r2["status"] == "no_close_match" and r2["projects"] == []


def test_list_json_replies_are_wrapped_not_crashing(tmp_path):
    class ListTriage(FakeLLM):
        def json(self, task, system, payload, role="primary", max_tokens=0):
            out = super().json(task, system, payload, role, max_tokens)
            if task == "triage":
                return out["picked"]  # bare JSON array
            return out

    s = Settings(fake=True, runs_dir=str(tmp_path))
    r = PriorArt(Clients(ListTriage(), FakeSearch()), s).run("A research agent that checks if an idea exists")
    assert r["status"] == "ok" and len(r["projects"]) >= 1


def test_markdown_escaping_and_url_scheme_safety():
    assert safe_url("javascript:alert(1)") == ""
    assert safe_url("data:text/html,<h1>x</h1>") == ""
    assert safe_url("https://example.com/ok") == "https://example.com/ok"
    escaped = md_escape("![](https://evil.test/px) | [phish](https://evil.test)\n<b>hi</b>", table=True)
    assert "\\!\\[\\]" in escaped and "\\|" in escaped and "\n" not in escaped and "\\<b\\>" in escaped

    fake_report = {
        "status": "ok",
        "message": "1 of 2 pages couldn't be read",
        "idea": "Idea with <script>",
        "novelty": 50,
        "verdict": "Some verdict",
        "features": [{"id": "F1", "name": "Feat|1", "description": "desc"}],
        "projects": [{
            "n": 1, "name": "Bad|Proj", "cells": {
                "F1": {"score": 2, "status": "verified", "quote": "![](https://track.example/p.png)", "reason": ""}
            }
        }],
        "coverage": {"F1": 2},
        "checks": {"checked": 1, "cut_quote": 0, "cut_verifier": 0, "downgraded": 0},
        "gaps": [],
        "pivots": [],
        "sources": [{"n": 1, "name": "Bad", "source": "Web", "url": "javascript:alert(1)"}],
        "candidates_found": 1,
        "models": {"primary": "m1", "verifier": "m2"},
        "seconds": 1.0,
    }
    md = to_markdown(fake_report)
    assert "1 of 2 pages couldn't be read" in md
    assert "javascript:" not in md
    assert "(invalid URL omitted)" in md
    assert "Bad\\|Proj" in md


# ---------- 2. Config validation, resilience, cache & LLM parsing ----------

def test_settings_validation_rejects_bad_bounds_and_prod_guards():
    with pytest.raises(ConfigError, match="PRIORART_WORKERS"):
        Settings(workers=0).validate()
    with pytest.raises(ConfigError, match="PRIORART_MAX_CANDIDATES"):
        Settings(max_candidates=80).validate()
    with pytest.raises(ConfigError, match="PRIORART_ENV"):
        Settings(env="mars").validate()
    with pytest.raises(ConfigError, match="PRIORART_FAKE"):
        Settings(env="production", fake=True, nebius_api_key="a", tavily_api_key="b").validate()
    with pytest.raises(ConfigError, match="PRIORART_SIMULATE"):
        Settings(env="production", simulate="search_down", nebius_api_key="a", tavily_api_key="b").validate()
    with pytest.raises(ConfigError, match="NEBIUS_API_KEY"):
        Settings(env="production", fake=False, nebius_api_key="", tavily_api_key="b").validate()
    with pytest.raises(ConfigError, match="PRIORART_RUNS_DIR"):
        Settings(runs_dir="/").validate()


def test_retry_call_no_sleep_after_final_attempt(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    calls = 0

    def always_fail():
        nonlocal calls
        calls += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        retry_call(always_fail, attempts=3, is_retryable=lambda e: True)
    assert calls == 3 and len(sleeps) == 2  # slept between 1->2 and 2->3, NEVER after 3


def test_circuit_breaker_lifecycle():
    now = [0.0]
    cb = CircuitBreaker("test", failure_threshold=2, window_s=30.0, cooldown_s=60.0, clock=lambda: now[0])
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"
    with pytest.raises(ProviderUnavailable):
        cb.before_call()
    now[0] = 61.0
    assert cb.state == "half_open"
    cb.before_call()
    cb.record_success()
    assert cb.state == "closed"


def test_check_budget_enforces_caps():
    b = CheckBudget(max_model_calls=2, max_tavily_credits=3.0)
    b.reserve_model_call(input_chars=100, max_tokens=500)
    b.reserve_model_call(input_chars=100, max_tokens=500)
    with pytest.raises(BudgetExceeded):
        b.reserve_model_call(input_chars=100, max_tokens=500)
    b.reserve_tavily(2.0)
    with pytest.raises(BudgetExceeded):
        b.reserve_tavily(2.0)


def test_cache_concurrency_single_flight_and_unwritable_fallback(tmp_path):
    cache = Cache(str(tmp_path / "c.sqlite"))
    calls = 0
    lock = threading.Lock()

    def slow_compute():
        nonlocal calls
        time.sleep(0.05)
        with lock:
            calls += 1
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: cache.get_or_compute("llm", {"k": 1}, slow_compute), range(8)))
    assert calls == 1
    assert all(v == {"ok": True} for v, _ in results)
    cache.close()

    # Unwritable path degrades gracefully instead of crashing.
    bad = Cache("/nonexistent_root_dir_zzz/sub/cache.sqlite", enabled=True)
    assert bad.enabled is False
    val, cached = bad.get_or_compute("llm", {"k": 1}, lambda: {"still": "works"})
    assert val == {"still": "works"} and cached is False


def test_search_extract_caches_per_url_across_reordered_batches(tmp_path):
    s = Settings(nebius_api_key="k", tavily_api_key="t")
    cache = Cache(str(tmp_path / "c.sqlite"))
    srch = Search(s, cache)
    calls: list[list[str]] = []

    class FakeTavily:
        def extract(self, **kw):
            calls.append(list(kw["urls"]))
            return {"results": [{"url": u, "raw_content": f"body of {u}"} for u in kw["urls"]]}

    srch.client = FakeTavily()
    p1 = srch.extract(["https://example.com/a", "https://example.com/b"])
    p2 = srch.extract(["https://example.com/b", "https://example.com/a", "https://example.com/c"])
    assert len(p1) == 2 and len(p2) == 3
    assert calls == [["https://example.com/a", "https://example.com/b"], ["https://example.com/c"]]
    cache.close()


def test_parse_json_handles_stray_braces_and_unclosed_think():
    assert parse_json('Note {ignore} here: {"a": 1}') == {"a": 1}
    assert parse_json('<think>reasoning\n{"ok": true}') == {"ok": True}
    with pytest.raises(LLMError):
        parse_json("no json at all")


# ---------- 3. Database hardening & ReportStore (N5, N9, N10, S.04) ----------

def test_migrate_block_comment_cannot_bypass_lint(tmp_path):
    p = tmp_path / "0001_bad.up.sql"
    p.write_text("/* bypass */ BEGIN;\n/* bypass */ PRAGMA foreign_keys = OFF;")
    with pytest.raises(MigrationError):
        lint(p)


def test_pool_write_rolls_back_stale_transaction_on_exception(tmp_path):
    db = tmp_path / "p.sqlite"
    c = connect(db)
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    c.close()

    pool = Pool(db, readers=2)
    try:
        with pytest.raises(RuntimeError):
            with pool.write() as w:
                w.execute("BEGIN IMMEDIATE")
                w.execute("INSERT INTO owners (token_hash) VALUES (?)", ("a" * 64,))
                raise RuntimeError("crash mid-transaction")
        # Next writer must get a clean connection and zero partial rows.
        with pool.write() as w:
            assert not w.in_transaction
            w.execute("BEGIN IMMEDIATE")
            assert w.execute("SELECT COUNT(*) FROM owners").fetchone() == (0,)
            w.execute("COMMIT")
    finally:
        pool.close()


def test_backup_restore_handles_special_chars_in_path(tmp_path):
    weird_dir = tmp_path / "dir?with#chars%20"
    weird_dir.mkdir()
    db = weird_dir / "live.sqlite"
    c = connect(db)
    Migrator(c, MIGRATIONS_DIR, log=lambda *_: None).up()
    c.close()
    bk = weird_dir / "bk.sqlite"
    info = backup(db, bk)
    assert info["integrity"] == "ok" and info["schema_version"] == 5
    rinfo = restore(bk, db)
    assert rinfo["integrity"] == "ok"


def test_report_store_persists_real_pipeline_and_recovers_stale_running(tmp_path):
    store = ReportStore(tmp_path / "app.sqlite")
    try:
        oid = store.ensure_owner("1" * 64)
        other_oid = store.ensure_owner("2" * 64)

        # Full pipeline result (including verified claims with non-empty reasons!) persists without IntegrityError.
        s = Settings(fake=True, runs_dir=str(tmp_path / "runs"))
        agent = build(s)
        rid, created = store.create_running_report(
            owner_id=oid, idea="A research agent that checks if an idea exists",
            writer_model="fake/primary-model", verifier_model="fake/verifier-model",
            client_request_id="idem-123",
        )
        assert created is True
        # Same idempotency key returns same report id.
        rid2, created2 = store.create_running_report(
            owner_id=oid, idea="A research agent that checks if an idea exists",
            writer_model="fake/primary-model", verifier_model="fake/verifier-model",
            client_request_id="idem-123",
        )
        assert rid2 == rid and created2 is False

        result = agent.run("A research agent that checks if an idea exists", report_id=rid, save_file=False)
        store.finish_report(rid, oid, result, candidates=agent._last_candidates)

        # Hydrate via owner A succeeds; owner B gets None.
        hydrated = store.get_report(rid, oid)
        assert hydrated is not None and hydrated["status"] == "ok" and len(hydrated["projects"]) == 5
        assert store.get_report(rid, other_oid) is None

        # Cross-owner pivot isolation check (S.04 #07)
        piv_id, _ = store.create_running_report(
            owner_id=other_oid, idea="A completely independent pivot idea to check",
            writer_model="fake/primary-model", verifier_model="fake/verifier-model",
            pivot_of_report_id=rid,  # different owner -> ignored!
        )
        with store.pool.read() as conn:
            rows_a = conn.execute(QUERIES["pivots_of_report"].sql, params_for(QUERIES["pivots_of_report"], rid, oid)).fetchall()
            assert rows_a == []

        # Stale running report recovery (S.04 #09)
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with store.pool.write() as conn:
            conn.execute("UPDATE reports SET heartbeat_at = ?, started_at = ? WHERE id = ?", (old_ts, old_ts, piv_id))
        recovered = store.recover_stale_running(max_age_minutes=10)
        assert recovered == 1
        rec_rep = store.get_report(piv_id, other_oid)
        assert rec_rep is not None and rec_rep["status"] == "failed" and "restarted" in rec_rep["message"]
    finally:
        store.close()


# ---------- 4. Server security, owner isolation, rate limits, health & UI invariants ----------

def _app_client(tmp_path: Path, **overrides) -> TestClient:
    kwargs = dict(
        fake=True,
        runs_dir=str(tmp_path / "runs"),
        db_path=str(tmp_path / "app.sqlite"),
        cache_path=str(tmp_path / "cache.sqlite"),
        rate_limit_10m=10,
        rate_limit_day=50,
        rate_limit_global_hour=100,
    )
    kwargs.update(overrides)
    return TestClient(create_app(Settings(**kwargs)))


def test_server_security_headers_dns_rebinding_and_cross_site(tmp_path):
    with _app_client(tmp_path) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "unsafe-inline" not in r.headers["content-security-policy"]
        assert r.headers["x-request-id"]
        assert r.headers["x-prior-art-version"]

        # DNS rebinding blocked (S.10 #06)
        bad_host = c.get("/", headers={"Host": "evil.example"})
        assert bad_host.status_code == 400

        # Cross-site POST blocked (S.10 #16)
        xsite = c.post(
            "/api/checks",
            json={"idea": "A research agent that checks ideas"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert xsite.status_code == 403

        # Health & version endpoints (S.10 #19)
        assert c.get("/health/live").json() == {"status": "ok"}
        ready = c.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["components"]["database"] == "ok"
        assert "version" in c.get("/version").json()


def test_server_owner_isolation_idempotency_and_lifecycle(tmp_path):
    app = create_app(Settings(
        fake=True,
        runs_dir=str(tmp_path / "runs"),
        db_path=str(tmp_path / "app.sqlite"),
        cache_path=str(tmp_path / "cache.sqlite"),
        rate_limit_10m=10,
    ))
    with TestClient(app) as client_a, TestClient(app) as client_b:
        r1 = client_a.post(
            "/api/checks",
            json={"idea": "A research agent that checks if an idea exists"},
            headers={"Idempotency-Key": "key-abc"},
        )
        assert r1.status_code == 202
        rid = r1.json()["id"]

        # Same idempotency key returns same check id without launching a duplicate check.
        r2 = client_a.post(
            "/api/checks",
            json={"idea": "A research agent that checks if an idea exists"},
            headers={"Idempotency-Key": "key-abc"},
        )
        assert r2.status_code == 202 and r2.json()["id"] == rid

        # Wait for stream completion
        with client_a.stream("GET", f"/api/checks/{rid}/events") as resp:
            stream_body = "".join(resp.iter_text())
        assert "event: done" in stream_body

        # Owner B gets 404 on every endpoint for Owner A's report and sees empty list.
        assert client_b.get("/api/reports").json() == []
        assert client_b.get(f"/api/reports/{rid}").status_code == 404
        assert client_b.get(f"/api/reports/{rid}/report.md").status_code == 404
        assert client_b.get(f"/api/checks/{rid}/events").status_code == 404
        assert client_b.delete(f"/api/reports/{rid}").status_code == 404

        # Owner A soft-deletes, restores, exports, and deletes own data.
        assert client_a.delete(f"/api/reports/{rid}").status_code == 200
        assert client_a.get(f"/api/reports/{rid}").status_code == 404
        assert client_a.post(f"/api/reports/{rid}/restore").status_code == 200
        assert client_a.get(f"/api/reports/{rid}").status_code == 200
        exp = client_a.get("/api/me/export")
        assert exp.status_code == 200 and len(exp.json()["reports"]) == 1
        assert client_a.delete("/api/me").status_code == 200
        assert client_a.get("/api/reports").json() == []


def test_server_rate_limit_and_production_guards(tmp_path):
    with _app_client(tmp_path, rate_limit_10m=1, rate_limit_day=10) as c:
        r1 = c.post("/api/checks", json={"idea": "First unique startup idea description"})
        assert r1.status_code == 202
        with c.stream("GET", f"/api/checks/{r1.json()['id']}/events") as resp:
            "".join(resp.iter_text())
        r2 = c.post("/api/checks", json={"idea": "Second completely different startup idea"})
        assert r2.status_code == 429 and "retry-after" in r2.headers

    # Production mode disables /docs, /openapi.json, simulate, and adds HSTS.
    prod_settings = Settings(
        env="production",
        fake=False,
        nebius_api_key="real-looking-key-123",
        tavily_api_key="tvly-real-looking-key",
        runs_dir=str(tmp_path / "p_runs"),
        db_path=str(tmp_path / "p_app.sqlite"),
        cache_path=str(tmp_path / "p_cache.sqlite"),
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    with TestClient(create_app(prod_settings)) as pc:
        assert pc.get("/docs").status_code == 404
        assert pc.get("/openapi.json").status_code == 404
        st = pc.get("/api/status").json()
        assert st["demo_controls"] is False and "missing" not in st
        assert pc.post("/api/checks", json={"idea": "An idea in production mode", "simulate": "search_down"}).status_code == 400
        assert "strict-transport-security" in pc.get("/").headers


def test_index_html_csp_no_inline_styles_and_noreferrer():
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Strict CSP compatibility: zero style="..." attributes anywhere in index.html.
    assert 'style="' not in html
    # Every target="_blank" link must carry rel="noopener noreferrer".
    for m in re.finditer(r'target="_blank"[^>]*>', html):
        assert 'rel="noopener noreferrer"' in m.group(0), m.group(0)
    # CSP header includes concrete sha256 hashes for both style and script.
    csp = compute_csp_header(INDEX_HTML)
    assert csp.count("'sha256-") >= 2 and "unsafe-inline" not in csp


def test_log_redaction_and_error_capture():
    cleaned = redact({"api_key": "secret123", "idea": "top secret startup", "note": "Bearer tok_abc tvly-12345678 a@b.com"})
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["idea"] == "[REDACTED]"
    assert "tok_abc" not in cleaned["note"] and "tvly-12345678" not in cleaned["note"] and "a@b.com" not in cleaned["note"]
    ref = capture_error(RuntimeError("Bearer secret_token_xyz"), layer="test")
    assert ref.startswith("err_")
    fmt = JsonFormatter(env="test")
    assert fmt.env == "test"


def test_cli_in_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRIORART_FAKE", "1")
    monkeypatch.setenv("PRIORART_RUNS_DIR", str(tmp_path / "runs"))
    assert cli_main(["config", "check"]) == 0
    cfg_out = json.loads(capsys.readouterr().out)
    assert cfg_out["fake"] is True

    md = tmp_path / "out.md"
    rc = cli_main(["A research agent that checks if an idea exists", "--md", str(md), "--quiet"])
    assert rc == 0 and md.exists()
    assert "NOVELTY" in capsys.readouterr().out

    # --json output parses
    assert cli_main(["A research agent that checks if an idea exists", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "ok"

    # --file reads UTF-8 file cleanly
    idea_file = tmp_path / "idea.txt"
    idea_file.write_text("An offline-first multilingual household note app with RTL support.", encoding="utf-8")
    assert cli_main(["--file", str(idea_file), "--quiet"]) == 0
    capsys.readouterr()

    # Over-length idea rejected by core clean_idea()
    with pytest.raises(ValueError, match="4,000"):
        clean_idea("x" * 4001)

    # Bad config exits with code 2
    monkeypatch.setenv("PRIORART_WORKERS", "999")
    assert cli_main(["config", "check"]) == 2


# ---------- 5. Deep resilience, repair, budget, SSE replay & usage ledger coverage ----------

def test_llm_repair_call_and_error_branches(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx
    import openai

    from priorart.llm import LLM

    monkeypatch.setattr("time.sleep", lambda _s: None)
    s = Settings(nebius_api_key="k", tavily_api_key="t")
    cache = Cache(str(tmp_path / "c.sqlite"))
    llm = LLM(s, cache)

    # First reply is unparseable prose; repair returns valid JSON object.
    replies = ["Sorry, I forgot JSON.", '{"picked": [{"id": "c1"}]}']
    calls: list[dict] = []

    class RepairComp:
        def create(self, **kw):
            calls.append(kw)
            text = replies.pop(0)
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage)

    budget = CheckBudget()
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=RepairComp()))
    out = llm.json("triage", "sys", {"x": 1}, budget=budget)
    assert out == {"picked": [{"id": "c1"}]}
    assert len(calls) == 2
    assert budget.prompt_tokens == 20 and budget.completion_tokens == 10

    # 400 "invalid model" fails fast with model hint
    req = httpx.Request("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions")
    bad_model = openai.BadRequestError("invalid model xyz", response=httpx.Response(400, request=req), body=None)

    class BadModelComp:
        def create(self, **kw):
            raise bad_model

    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=BadModelComp()))
    with pytest.raises(LLMError, match="agent.py models"):
        llm.json("decompose", "sys", {"x": 2})
    llm.close()
    cache.close()


def test_search_retries_429_records_usage_and_filters_bad_urls(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    s = Settings(nebius_api_key="k", tavily_api_key="t")
    cache = Cache(str(tmp_path / "c.sqlite"))
    srch = Search(s, cache)
    attempts = 0

    class FlakyTavily:
        def search(self, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("429 rate limit")
            return {
                "usage": {"credits": 2},
                "results": [
                    {"title": "Good", "url": "https://github.com/a/b", "content": "ok", "score": 0.9},
                    {"title": "Evil", "url": "javascript:alert(1)", "content": "xss", "score": 0.99},
                    {"title": None, "url": "https://example.com/n", "content": None, "score": None},
                ],
            }

    srch.client = FlakyTavily()
    budget = CheckBudget()
    res = srch.search("github", "q", budget=budget)
    assert attempts == 2
    assert len(res) == 2
    assert all(r["url"].startswith("https://") for r in res)
    assert budget.tavily_credits == 2.0
    # Second call hits cache
    srch.search("github", "q", budget=budget)
    assert budget.cache_hits == 1
    cache.close()


def test_store_usage_ledger_budget_and_usage_cli(tmp_path):
    import subprocess
    import sys

    from priorart.resilience import UsageRecord
    from priorart.store import _db_overlap_fields

    # Verify all overlap coercion edge cases satisfy schema.sql rules cleanly
    assert _db_overlap_fields({"claimed": 2, "score": 0, "status": "cut_quote", "quote": "", "reason": ""})[2] == "(no quote provided)"
    assert _db_overlap_fields({"claimed": 2, "score": 2, "status": "verified", "quote": "end-to-end encryption", "reason": "ignored"})[4] == ""
    assert _db_overlap_fields({"claimed": 2, "score": 2, "status": "downgraded", "quote": "a b c", "reason": ""})[1] == 1
    assert _db_overlap_fields({"claimed": 1, "score": 1, "status": "downgraded", "quote": "a b c", "reason": ""})[3] == "cut_verifier"
    assert _db_overlap_fields({"claimed": 2, "score": 2, "status": "verified", "quote": "多言語ノート", "reason": ""})[2].count(" ") >= 2

    db = tmp_path / "usage.sqlite"
    store = ReportStore(db)
    try:
        store.record_usage(UsageRecord(provider="nebius", operation="decompose", model="m1", prompt_tokens=100, completion_tokens=250), check_id="AbCdEf1234")
        store.record_usage(UsageRecord(provider="tavily", operation="search", credits=2.0), check_id="AbCdEf1234")
        store.record_usage(UsageRecord(provider="tavily", operation="search", credits=2.0, cached=True), check_id="AbCdEf1234")
        within, u = store.check_daily_budget(max_credits=10.0, max_output_tokens=1000)
        assert within is True and u["credits"] == 2.0 and u["completion_tokens"] == 250 and u["cache_hits"] == 1
        over, _ = store.check_daily_budget(max_credits=1.0, max_output_tokens=1000)
        assert over is False
        # Verify fail_running_report path
        oid = store.ensure_owner("9" * 64)
        rid, _ = store.create_running_report(owner_id=oid, idea="A test idea that will fail early", writer_model="w", verifier_model="v")
        store.append_event(rid, oid, 1, "stage", {"stage": "decompose"})
        assert len(store.events_after(rid, oid, after_seq=0) or []) == 1
        store.fail_running_report(rid, oid, "boom")
        failed = store.get_report(rid, oid)
        assert failed is not None and failed["status"] == "failed"
    finally:
        store.close()

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, "scripts/usage.py", "--db", str(db)], cwd=root, capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and "Prior Art usage" in proc.stdout and "nebius/decompose" in proc.stdout


def test_server_sse_replay_spa_route_and_worker_error_masking(tmp_path, monkeypatch):
    with _app_client(tmp_path) as c:
        r = c.post("/api/checks", json={"idea": "A research agent that checks if an idea exists"})
        assert r.status_code == 202
        rid = r.json()["id"]
        # Deep link SPA route returns HTML
        page = c.get(f"/r/{rid}")
        assert page.status_code == 200 and "Prior Art" in page.text

        with c.stream("GET", f"/api/checks/{rid}/events") as resp:
            first_stream = "".join(resp.iter_text())
        assert "event: done" in first_stream

        # Reconnecting with Last-Event-ID replays terminal state cleanly without starting new work
        with c.stream("GET", f"/api/checks/{rid}/events", headers={"Last-Event-ID": "999"}) as resp:
            replay = "".join(resp.iter_text())
        assert "event: done" in replay

        # Soft-delete appears in /api/reports/deleted
        assert c.delete(f"/api/reports/{rid}").status_code == 200
        deleted = c.get("/api/reports/deleted").json()
        assert any(d["id"] == rid for d in deleted)

    # Worker crash emits check_error with opaque ref, never raw secret/exception text
    def exploding_build(*args, **kwargs):
        raise RuntimeError("SECRET_INTERNAL_PATH /private/var/secret.key")

    monkeypatch.setattr("priorart.server.build", exploding_build)
    with _app_client(tmp_path / "err") as c:
        r = c.post("/api/checks", json={"idea": "An idea that triggers an internal worker crash"})
        assert r.status_code == 202
        rid = r.json()["id"]
        with c.stream("GET", f"/api/checks/{rid}/events") as resp:
            body = "".join(resp.iter_text())
        assert "event: check_error" in body
        assert "SECRET_INTERNAL_PATH" not in body
        assert "err_" in body


def test_cli_models_and_missing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRIORART_FAKE", "1")
    monkeypatch.setenv("PRIORART_RUNS_DIR", str(tmp_path / "runs"))
    assert cli_main(["models"]) == 0
    assert "fake/primary-model" in capsys.readouterr().out
    assert cli_main(["--file", str(tmp_path / "does-not-exist.txt")]) == 1
    assert "Error reading" in capsys.readouterr().err


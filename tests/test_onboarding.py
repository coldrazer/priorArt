"""Comprehensive tests for the Prior Art Onboarding & Settings Wizard (backend, API, security, CLI)."""
from __future__ import annotations

import io
import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from priorart.__main__ import main as cli_main
from priorart.config import ConfigError, Settings
from priorart.onboarding import (
    apply_setup_update,
    format_env_value,
    is_key_set,
    mask_secret,
    model_family,
    run_cli_wizard,
    setup_payload,
    verify_connections,
    write_env_file,
)
from priorart.server import INDEX_HTML, _require_local_setup_allowed, create_app


def _base_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        env="development",
        fake=False,
        nebius_api_key="",
        tavily_api_key="",
        db_path=str(tmp_path / "app.sqlite"),
        cache_path=str(tmp_path / "cache.sqlite"),
        runs_dir=str(tmp_path / "runs"),
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_secret_masking_and_env_formatting():
    assert is_key_set("") is False
    assert is_key_set("your-token-factory-key") is False
    assert is_key_set("tvly-abc12345678") is True
    assert mask_secret("") == ""
    assert mask_secret("your-tavily-key") == ""
    assert mask_secret("short") == "***"
    assert mask_secret("tvly-abc12345678") == "***5678"
    assert model_family("meta-llama/Llama-3.3-70B-Instruct") == "meta-llama"

    assert format_env_value("simple-value") == "simple-value"
    assert format_env_value("has space # comment") == '"has space # comment"'
    assert format_env_value('a"b') == '"a\\"b"'
    with pytest.raises(ConfigError):
        format_env_value("bad\nINJECTED=1")
    with pytest.raises(ConfigError):
        format_env_value("bad\x00null")


def test_write_env_file_atomic_permissions_and_preservation(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# User comment\nCUSTOM_VAR=keep-me\nNEBIUS_API_KEY=old-key\nPRIORART_MAX_CANDIDATES=4\n",
        encoding="utf-8",
    )
    s = _base_settings(
        tmp_path,
        nebius_api_key="neb-secret-9999",
        tavily_api_key="tvly-secret-1234",
        max_candidates=10,
        allowed_hosts=["127.0.0.1", "localhost", "testserver", "prior.example"],
        public_origin="https://prior.example",
    )
    written = write_env_file(s, env_file)
    assert written == env_file
    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600

    text = env_file.read_text(encoding="utf-8")
    assert "# User comment" in text
    assert "CUSTOM_VAR=keep-me" in text
    assert "NEBIUS_API_KEY=neb-secret-9999" in text
    assert "TAVILY_API_KEY=tvly-secret-1234" in text
    assert "PRIORART_MAX_CANDIDATES=10" in text
    assert "PRIORART_ALLOWED_HOSTS=prior.example" in text
    assert "PRIORART_PUBLIC_ORIGIN=https://prior.example" in text
    assert text.count("NEBIUS_API_KEY=") == 1

    # Clearing a key removes its line cleanly.
    s_cleared, _ = apply_setup_update(s, {"clear_nebius_key": True})
    write_env_file(s_cleared, env_file)
    text2 = env_file.read_text(encoding="utf-8")
    assert "NEBIUS_API_KEY=" not in text2
    assert "TAVILY_API_KEY=tvly-secret-1234" in text2
    assert "CUSTOM_VAR=keep-me" in text2


def test_apply_setup_update_validation_and_restart_detection(tmp_path):
    cur = _base_settings(tmp_path, nebius_api_key="neb-existing-1111", tavily_api_key="tvly-existing-2222")

    # Blank key fields preserve existing secrets.
    updated, restart = apply_setup_update(
        cur,
        {
            "nebius_api_key": "",
            "tavily_api_key": None,
            "primary_model": "deepseek-ai/DeepSeek-V3",
            "verifier_model": "Qwen/Qwen3-235B-A22B-Instruct-2507",
            "max_candidates": 6,
            "rate_limit_10m": 5,
            "daily_budget_credits": 8000,
        },
    )
    assert updated.nebius_api_key == "neb-existing-1111"
    assert updated.tavily_api_key == "tvly-existing-2222"
    assert updated.primary_model == "deepseek-ai/DeepSeek-V3"
    assert updated.max_candidates == 6
    assert updated.rate_limit_10m == 5
    assert updated.daily_budget_credits == 8000
    assert restart == []

    # Changing db_path or allowed_hosts flags restart_required.
    updated2, restart2 = apply_setup_update(
        cur,
        {
            "db_path": str(tmp_path / "other.sqlite"),
            "allowed_hosts": "demo.example, app.local",
        },
    )
    assert set(restart2) == {"db_path", "allowed_hosts"}
    assert "demo.example" in updated2.allowed_hosts
    assert "app.local" in updated2.allowed_hosts

    # Validation errors: invalid URL, out-of-range int, unsafe dir, newline injection.
    with pytest.raises(ConfigError):
        apply_setup_update(cur, {"nebius_base_url": "file:///etc/passwd"})
    with pytest.raises(ConfigError):
        apply_setup_update(cur, {"public_origin": "javascript:alert(1)"})
    with pytest.raises(ConfigError):
        apply_setup_update(cur, {"max_candidates": 999})
    with pytest.raises(ConfigError):
        apply_setup_update(cur, {"runs_dir": "/"})
    with pytest.raises(ConfigError):
        apply_setup_update(cur, {"nebius_api_key": "abc\nTAVILY_API_KEY=evil"})


def test_setup_payload_masks_secrets(tmp_path):
    s = _base_settings(tmp_path, nebius_api_key="neb-super-secret-4321", tavily_api_key="tvly-super-secret-8765")
    p = setup_payload(s, tmp_path / ".env")
    raw = str(p)
    assert "neb-super-secret-4321" not in raw
    assert "tvly-super-secret-8765" not in raw
    assert p["nebius_api_key_set"] is True
    assert p["nebius_api_key_masked"] == "***4321"
    assert p["tavily_api_key_set"] is True
    assert p["tavily_api_key_masked"] == "***8765"
    assert p["needs_onboarding"] is False
    assert "max_candidates" in p["bounds"]


def test_verify_connections_mocked_and_redacted(monkeypatch):
    # Unset keys
    res_empty = verify_connections(
        nebius_api_key="",
        tavily_api_key="",
        nebius_base_url="https://api.tokenfactory.nebius.com/v1/",
    )
    assert res_empty["nebius"]["ok"] is False and res_empty["nebius"]["tested"] is False
    assert res_empty["tavily"]["ok"] is False and res_empty["tavily"]["tested"] is False

    # Invalid URL
    res_bad_url = verify_connections(
        nebius_api_key="k1",
        tavily_api_key="k2",
        nebius_base_url="ftp://bad.invalid",
    )
    assert res_bad_url["nebius"]["ok"] is False

    # Mocked success
    import tavily as tavily_mod

    import priorart.llm as llm_mod

    fake_llm = MagicMock()
    fake_llm.list_models.return_value = ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen3-235B-A22B-Instruct-2507"]
    monkeypatch.setattr(llm_mod, "LLM", lambda *a, **kw: fake_llm)

    fake_tav = MagicMock()
    fake_tav.search.return_value = {"results": [{"url": "https://example.com"}]}
    monkeypatch.setattr(tavily_mod, "TavilyClient", lambda api_key: fake_tav)

    res_ok = verify_connections(
        nebius_api_key="neb-valid-key-9999",
        tavily_api_key="tvly-valid-key-8888",
        nebius_base_url="https://api.tokenfactory.nebius.com/v1/",
        primary_model="meta-llama/Llama-3.3-70B-Instruct",
        verifier_model="Qwen/Qwen3-235B-A22B-Instruct-2507",
    )
    assert res_ok["nebius"]["ok"] is True
    assert len(res_ok["nebius"]["models"]) == 2
    assert res_ok["nebius"]["primary_available"] is True
    assert res_ok["tavily"]["ok"] is True

    # Mocked failure redacts leaked tokens from exception messages
    bad_llm = MagicMock()
    bad_llm.list_models.side_effect = RuntimeError("401 Unauthorized Bearer secret_tok_abc123")
    monkeypatch.setattr(llm_mod, "LLM", lambda *a, **kw: bad_llm)

    bad_tav = MagicMock()
    bad_tav.search.side_effect = RuntimeError("Invalid key tvly-leakedsecret12345")
    monkeypatch.setattr(tavily_mod, "TavilyClient", lambda api_key: bad_tav)

    res_err = verify_connections(
        nebius_api_key="neb-bad-key-1111",
        tavily_api_key="tvly-leakedsecret12345",
        nebius_base_url="https://api.tokenfactory.nebius.com/v1/",
    )
    assert res_err["nebius"]["ok"] is False
    assert "secret_tok_abc123" not in res_err["nebius"]["message"]
    assert res_err["tavily"]["ok"] is False
    assert "tvly-leakedsecret12345" not in res_err["tavily"]["message"]


def test_setup_api_lifecycle_hot_reload_and_security(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    # Keep process env clean during test.
    for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "PRIORART_FAKE", "PRIORART_MAX_CANDIDATES"):
        monkeypatch.delenv(key, raising=False)

    s = _base_settings(tmp_path)
    app = create_app(s, env_path=env_file)

    with TestClient(app) as client:
        # Initially unconfigured -> needs_onboarding=True
        st0 = client.get("/api/status").json()
        assert st0["setup_enabled"] is True
        assert st0["needs_onboarding"] is True
        assert st0["available"] is False

        # GET /api/setup returns masked config with no-store cache header
        g = client.get("/api/setup")
        assert g.status_code == 200
        assert g.headers.get("cache-control") == "no-store"
        body = g.json()
        assert body["needs_onboarding"] is True
        assert body["nebius_api_key_set"] is False

        # CSRF cross-site POST rejected
        cross = client.post(
            "/api/setup",
            headers={"Sec-Fetch-Site": "cross-site"},
            json={"fake": True},
        )
        assert cross.status_code == 403

        # Invalid payload rejected with 422
        bad = client.post("/api/setup", json={"max_candidates": 99})
        assert bad.status_code == 422

        # Save keys + models + tuning -> writes .env (0600) and hot-reloads server!
        saved = client.post(
            "/api/setup",
            json={
                "nebius_api_key": "neb-live-key-7777",
                "tavily_api_key": "tvly-live-key-8888",
                "primary_model": "deepseek-ai/DeepSeek-V3",
                "verifier_model": "Qwen/Qwen3-235B-A22B-Instruct-2507",
                "max_candidates": 6,
                "rate_limit_10m": 7,
                "daily_budget_credits": 9000,
                "fake": False,
            },
        )
        assert saved.status_code == 200
        assert saved.headers.get("cache-control") == "no-store"
        sdata = saved.json()
        assert sdata["saved"] is True
        assert sdata["nebius_api_key_set"] is True
        assert sdata["nebius_api_key_masked"] == "***7777"
        assert sdata["tavily_api_key_masked"] == "***8888"
        assert "neb-live-key-7777" not in saved.text
        assert "tvly-live-key-8888" not in saved.text
        assert env_file.exists()
        assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600

        # /api/status reflects live hot reload immediately!
        st1 = client.get("/api/status").json()
        assert st1["available"] is True
        assert st1["needs_onboarding"] is False
        assert st1["primary_model"] == "deepseek-ai/DeepSeek-V3"

        # Enable demo mode via /api/setup and verify a live check succeeds immediately via hot reload
        demo_resp = client.post("/api/setup", json={"fake": True})
        assert demo_resp.status_code == 200
        assert demo_resp.json()["fake"] is True

        chk = client.post("/api/checks", json={"idea": "An automated onboarding wizard for prior art checks"})
        assert chk.status_code == 202
        rid = chk.json()["id"]
        with client.stream("GET", f"/api/checks/{rid}/events") as stream:
            raw_sse = "".join(stream.iter_text())
        assert "event: done" in raw_sse

        # POST /api/setup/test uses mocked verify_connections cleanly
        import priorart.server as srv_mod

        monkeypatch.setattr(
            srv_mod,
            "verify_connections",
            lambda **kw: {
                "nebius": {"ok": True, "tested": True, "message": "Connected", "models": ["m/a"]},
                "tavily": {"ok": True, "tested": True, "message": "Connected"},
            },
        )
        tres = client.post("/api/setup/test", json={})
        assert tres.status_code == 200
        assert tres.headers.get("cache-control") == "no-store"
        assert tres.json()["nebius"]["ok"] is True


def test_setup_api_blocked_in_production_and_remote_hosts(tmp_path):
    prod = _base_settings(
        tmp_path,
        env="production",
        fake=False,
        nebius_api_key="neb-prod-1234",
        tavily_api_key="tvly-prod-5678",
    )
    with TestClient(create_app(prod, env_path=tmp_path / ".env")) as pc:
        st = pc.get("/api/status").json()
        assert st["setup_enabled"] is False
        assert st["needs_onboarding"] is False
        assert pc.get("/api/setup").status_code == 403
        assert pc.post("/api/setup", json={"max_candidates": 5}).status_code == 403
        assert pc.post("/api/setup/test", json={}).status_code == 403

    # Non-loopback client host blocked even in development mode
    dev = _base_settings(tmp_path)
    fake_req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"), headers={})
    with pytest.raises(HTTPException) as exc:
        _require_local_setup_allowed(fake_req, dev, mutate=False)  # type: ignore[arg-type]
    assert exc.value.status_code == 403


def test_cli_setup_wizard_interactive_and_cancel(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "PRIORART_FAKE", "PRIORART_MAX_CANDIDATES"):
        monkeypatch.delenv(key, raising=False)

    # Full live flow with advanced pipeline & limits customization
    answers = iter(
        [
            "n",  # offline demo mode? -> no
            "https://api.tokenfactory.nebius.com/v1/",  # base URL
            "n",  # test connections now? -> no
            "meta-llama/Llama-3.3-70B-Instruct",  # writer
            "Qwen/Qwen3-235B-A22B-Instruct-2507",  # verifier
            "y",  # customize pipeline?
            "6",  # max_candidates
            "5",  # results_per_query
            "8000",  # page_chars
            "6",  # workers
            "12",  # search_concurrency
            "8",  # model_concurrency
            "180",  # check_deadline_s
            "4",  # max_concurrent_checks
            "y",  # cache enabled
            "y",  # customize rate limits / budgets / storage?
            "4",  # rate_limit_10m
            "20",  # rate_limit_day
            "80",  # rate_limit_global_hour
            "6000",  # daily_budget_credits
            "3000000",  # daily_budget_output_tokens
            "myhost.local",  # allowed_hosts
            "https://myhost.local",  # public_origin
            "n",  # trust proxy headers
            str(tmp_path / "app.sqlite"),  # db_path
            str(tmp_path / "cache.sqlite"),  # cache_path
            str(tmp_path / "runs"),  # runs_dir
            "y",  # confirm save
        ]
    )
    secrets_iter = iter(["neb-cli-secret-5555", "tvly-cli-secret-6666"])
    buf = io.StringIO()
    rc = run_cli_wizard(
        env_path=env_file,
        input_fn=lambda _p: next(answers),
        getpass_fn=lambda _p: next(secrets_iter),
        out=buf,
    )
    assert rc == 0
    out_text = buf.getvalue()
    assert "***5555" in out_text and "neb-cli-secret-5555" not in out_text
    assert "***6666" in out_text and "tvly-cli-secret-6666" not in out_text
    saved_text = env_file.read_text(encoding="utf-8")
    assert "NEBIUS_API_KEY=neb-cli-secret-5555" in saved_text
    assert "PRIORART_MAX_CANDIDATES=6" in saved_text
    assert "PRIORART_DAILY_BUDGET_CREDITS=6000" in saved_text

    # Cancel flow via CLI dispatcher `agent.py setup --env-file ...`
    cancel_env = tmp_path / "cancel.env"
    cancel_answers = iter(["y", "", "", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(cancel_answers))
    rc2 = cli_main(["setup", "--env-file", str(cancel_env)])
    assert rc2 == 0
    assert not cancel_env.exists()


def test_index_html_has_onboarding_wizard_and_strict_csp():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="setup-dialog"' in html
    assert 'id="setup-banner"' in html
    for step in range(5):
        assert f'data-panel="{step}"' in html
    assert 'style="' not in html

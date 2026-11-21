"""Production Web UI & API server.

- Owner-scoped SQLite persistence (ReportStore + Pool + lifecycle)
- POST /api/checks (idempotent, rate-limited, concurrency-capped, same-origin)
- GET /api/checks/{id}/events (async SSE with Last-Event-ID replay and 15s heartbeats)
- Security headers (SHA-256 CSP, frame-ancestors, nosniff, no-referrer, HSTS in prod)
- TrustedHostMiddleware (blocks local DNS rebinding)
- Structured JSON request logs, error references, /health/live, /health/ready, /version
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from . import SIMULATE_MODES, VERSION, ConfigError, SharedRuntime, build
from .config import ROOT, Settings
from .log import (
    capture_error,
    check_id_var,
    configure_logging,
    get_logger,
    log_event,
    owner_ref_var,
    propagate_context,
    request_id_var,
)
from .onboarding import (
    apply_setup_update,
    setup_payload,
    sync_process_env,
    verify_connections,
    write_env_file,
)
from .pipeline import clean_idea, normalize_idea_key
from .report import to_markdown
from .resilience import BudgetExceeded, CheckBudget, ProviderUnavailable, UsageRecord
from .store import ReportStore, hash_token

STATIC = Path(__file__).parent / "static"
INDEX_HTML = STATIC / "index.html"
REPORT_ID_RE = re.compile(r"^[0-9A-Za-z]{10}$")
logger = get_logger("server")


# ---------- CSP hash computation from index.html ----------

def compute_csp_header(html_path: Path = INDEX_HTML) -> str:
    """Compute a strict CSP using exact SHA-256 hashes of the inline <style> and <script> blocks."""
    text = html_path.read_text(encoding="utf-8")
    style_hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(m.group(1).encode('utf-8')).digest()).decode('ascii')}'"
        for m in re.finditer(r"<style>(.*?)</style>", text, re.DOTALL)
    ]
    script_hashes = [
        f"'sha256-{base64.b64encode(hashlib.sha256(m.group(1).encode('utf-8')).digest()).decode('ascii')}'"
        for m in re.finditer(r"<script>(.*?)</script>", text, re.DOTALL)
    ]
    style_src = " ".join(style_hashes) if style_hashes else "'self'"
    script_src = " ".join(script_hashes) if script_hashes else "'self'"
    return (
        f"default-src 'none'; "
        f"script-src {script_src}; "
        f"style-src {style_src}; "
        f"img-src 'self' data:; "
        f"connect-src 'self'; "
        f"font-src 'self'; "
        f"base-uri 'none'; "
        f"form-action 'self'; "
        f"frame-ancestors 'none'"
    )


class _SelectiveGZipMiddleware:
    """Apply GZipMiddleware to normal responses, but bypass SSE event streams so chunks flush immediately."""

    def __init__(self, app: ASGIApp, minimum_size: int = 1024):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").endswith("/events"):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


# ---------- request models ----------

class CreateCheckRequest(BaseModel):
    idea: str = Field(..., min_length=1, max_length=4500)
    simulate: Literal["", "search_down", "extract_down"] | None = None
    pivot_of_report_id: str | None = None

    @field_validator("idea")
    @classmethod
    def _validate_idea(cls, v: str) -> str:
        return clean_idea(v)

    @field_validator("pivot_of_report_id")
    @classmethod
    def _validate_pivot_id(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not REPORT_ID_RE.match(v):
            raise ValueError("Invalid parent report id.")
        return v


class SetupUpdateRequest(BaseModel):
    nebius_api_key: str | None = None
    tavily_api_key: str | None = None
    clear_nebius_key: bool = False
    clear_tavily_key: bool = False
    nebius_base_url: str | None = None
    primary_model: str | None = None
    verifier_model: str | None = None
    fake: bool | None = None
    max_candidates: int | None = None
    results_per_query: int | None = None
    page_chars: int | None = None
    workers: int | None = None
    search_concurrency: int | None = None
    model_concurrency: int | None = None
    check_deadline_s: int | None = None
    max_concurrent_checks: int | None = None
    cache_enabled: bool | None = None
    rate_limit_10m: int | None = None
    rate_limit_day: int | None = None
    rate_limit_global_hour: int | None = None
    daily_budget_credits: int | None = None
    daily_budget_output_tokens: int | None = None
    allowed_hosts: str | list[str] | None = None
    public_origin: str | None = None
    trust_proxy_headers: bool | None = None
    db_path: str | None = None
    cache_path: str | None = None
    runs_dir: str | None = None


class SetupTestRequest(BaseModel):
    nebius_api_key: str | None = None
    tavily_api_key: str | None = None
    nebius_base_url: str | None = None
    primary_model: str | None = None
    verifier_model: str | None = None


# ---------- active check tracking & server state ----------

@dataclass
class _ActiveCheck:
    report_id: str
    owner_id: int
    idea_key: str
    started_at: float = field(default_factory=time.monotonic)
    done: bool = False
    seq: int = 0
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[tuple[int, str, dict[str, Any]] | None]]] = field(
        default_factory=list
    )

    def publish(self, kind: str, data: dict[str, Any]) -> int:
        with self.lock:
            self.seq += 1
            seq = self.seq
            subs = list(self.subscribers)
        for loop, q in subs:
            if not loop.is_closed():
                loop.call_soon_threadsafe(q.put_nowait, (seq, kind, data))
        return seq

    def finish(self) -> None:
        with self.lock:
            self.done = True
            subs = list(self.subscribers)
        for loop, q in subs:
            if not loop.is_closed():
                loop.call_soon_threadsafe(q.put_nowait, None)

    def subscribe(self) -> tuple[asyncio.Queue[tuple[int, str, dict[str, Any]] | None], bool]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[tuple[int, str, dict[str, Any]] | None] = asyncio.Queue()
        with self.lock:
            already_done = self.done
            if not already_done:
                self.subscribers.append((loop, q))
        return q, already_done

    def unsubscribe(self, q: asyncio.Queue[tuple[int, str, dict[str, Any]] | None]) -> None:
        with self.lock:
            self.subscribers = [(lp, sub_q) for lp, sub_q in self.subscribers if sub_q is not q]


class ServerState:
    def __init__(self, settings: Settings, *, env_path: Path | None = None):
        self.settings = settings
        self.env_path = env_path or (ROOT / ".env")
        self.store = ReportStore(settings.db_path)
        self.runtime = SharedRuntime(settings)
        self._retired_runtimes: list[SharedRuntime] = []
        self.semaphore = threading.BoundedSemaphore(settings.max_concurrent_checks)
        self.checks_lock = threading.Lock()
        self.active_checks: dict[str, _ActiveCheck] = {}
        self.shutting_down = False
        self.csp = compute_csp_header(INDEX_HTML)
        self.store.recover_stale_running()

    def reload_settings(self, new_settings: Settings) -> None:
        """Hot-reload live server settings without breaking in-flight checks."""
        new_rt = SharedRuntime(new_settings)
        old_rt: SharedRuntime | None = None
        close_now = False
        with self.checks_lock:
            has_running = any(not c.done for c in self.active_checks.values())
            old_rt = self.runtime
            self.settings = new_settings
            self.runtime = new_rt
            if not has_running:
                self.semaphore = threading.BoundedSemaphore(new_settings.max_concurrent_checks)
                close_now = True
            elif old_rt is not None:
                self._retired_runtimes.append(old_rt)
        if close_now and old_rt is not None:
            old_rt.close()

    def close(self) -> None:
        self.shutting_down = True
        with self.checks_lock:
            threads = [c.thread for c in self.active_checks.values() if c.thread is not None and c.thread.is_alive()]
            retired = list(self._retired_runtimes)
            self._retired_runtimes.clear()
        deadline = time.monotonic() + 10.0
        for th in threads:
            rem = max(0.0, deadline - time.monotonic())
            if rem > 0:
                th.join(timeout=rem)
        for rt in retired:
            rt.close()
        self.runtime.close()
        self.store.close()


# ---------- helpers ----------

def _cookie_name(request: Request, settings: Settings) -> str:
    is_https = request.url.scheme == "https" or (
        settings.trust_proxy_headers and request.headers.get("x-forwarded-proto") == "https"
    )
    return "__Host-pa_owner" if (settings.is_production or is_https) else "pa_owner"


def _read_owner_token(request: Request) -> str | None:
    return request.cookies.get("__Host-pa_owner") or request.cookies.get("pa_owner")


def _set_owner_cookie(request: Request, response: Response, settings: Settings, token: str) -> None:
    name = _cookie_name(request, settings)
    secure = name.startswith("__Host-")
    response.set_cookie(
        key=name,
        value=token,
        max_age=365 * 24 * 3600,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _daily_ip_hash(ip: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{day}:{ip}".encode("utf-8")).hexdigest()[:16]


def _enforce_same_origin(request: Request, settings: Settings) -> None:
    sfs = request.headers.get("sec-fetch-site", "").strip().lower()
    if sfs and sfs not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site requests are not allowed.")
    origin = request.headers.get("origin", "").strip().rstrip("/")
    if origin:
        expected_origins = set()
        if settings.public_origin:
            expected_origins.add(settings.public_origin)
        host = request.headers.get("host", "").strip()
        if host:
            expected_origins.add(f"http://{host}")
            expected_origins.add(f"https://{host}")
        if origin not in expected_origins:
            raise HTTPException(403, "Origin mismatch.")


def _require_local_setup_allowed(request: Request, settings: Settings, *, mutate: bool = False) -> None:
    """Restrict the onboarding/settings API to non-production loopback clients only."""
    if settings.is_production:
        raise HTTPException(
            403,
            "Onboarding wizard is disabled in production. Configure environment variables on your host.",
        )
    if mutate:
        _enforce_same_origin(request, settings)
    client_host = (request.client.host if request.client else "").strip().lower()
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Setup wizard is only accessible from localhost.")


def _validate_report_id(report_id: str) -> str:
    if not REPORT_ID_RE.match(report_id):
        raise HTTPException(404, "Report not found.")
    return report_id


def _sse_frame(seq: int, kind: str, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _friendly_error_message(exc: BaseException, ref: str) -> str:
    if isinstance(exc, BudgetExceeded):
        return f"This check hit its safety cost limit ({ref}). Try narrowing the idea or try again later."
    if isinstance(exc, ProviderUnavailable):
        return f"The {exc.provider} service is temporarily unavailable ({ref}). Please try again in a minute."
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, RuntimeError) and "Missing " in str(exc):
        return f"Prior Art isn't configured to run live checks right now ({ref})."
    return f"Something went wrong while running this check (ref {ref}). Your idea is still here; try again in a minute."


# ---------- app factory ----------

def create_app(settings: Settings | None = None, *, env_path: Path | None = None) -> FastAPI:
    cfg = settings if settings is not None else Settings.from_env(validate=True)
    cfg.validate()
    configure_logging(env=cfg.env)

    state_holder: dict[str, ServerState] = {}
    state_lock = threading.Lock()

    def get_state() -> ServerState:
        st = state_holder.get("state")
        if st is not None:
            return st
        with state_lock:
            st = state_holder.get("state")
            if st is None:
                st = ServerState(cfg, env_path=env_path)
                state_holder["state"] = st
            return st

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        get_state()
        try:
            yield
        finally:
            with state_lock:
                st = state_holder.pop("state", None)
            if st is not None:
                st.close()

    docs_kwargs: dict[str, Any] = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None} if cfg.is_production else {}
    )
    app = FastAPI(title="Prior Art", version=VERSION, lifespan=lifespan, **docs_kwargs)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.allowed_hosts)
    app.add_middleware(_SelectiveGZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_and_logging_middleware(request: Request, call_next: Any) -> Response:
        req_id = request.headers.get("x-request-id", "").strip()[:64] or secrets.token_hex(8)
        tok_rid = request_id_var.set(req_id)
        t0 = time.monotonic()
        st = get_state()
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            ref = capture_error(exc, layer="http", request_id=req_id, path=request.url.path)
            response = JSONResponse(
                status_code=500,
                content={"detail": f"Something went wrong on our side (ref {ref}).", "ref": ref},
            )
        finally:
            dur_ms = round((time.monotonic() - t0) * 1000, 1)
            log_event(
                logger,
                logging.INFO,
                "request.completed",
                method=request.method,
                path=request.url.path,
                status=getattr(locals().get("response"), "status_code", 500),
                duration_ms=dur_ms,
            )
            request_id_var.reset(tok_rid)

        response.headers["X-Request-ID"] = req_id
        response.headers["X-Prior-Art-Version"] = VERSION
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = st.csp
        if st.settings.is_production or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def resolve_owner(request: Request, response: Response, *, create: bool) -> tuple[int | None, str | None]:
        st = get_state()
        raw_token = _read_owner_token(request)
        if raw_token:
            th = hash_token(raw_token)
            oid = st.store.ensure_owner(th) if create else st.store.owner_id_for_hash(th)
            if oid is not None:
                return oid, th[:12]
        if not create:
            return None, None
        new_token = secrets.token_urlsafe(32)
        th = hash_token(new_token)
        oid = st.store.ensure_owner(th)
        _set_owner_cookie(request, response, st.settings, new_token)
        return oid, th[:12]

    # ---------- pages ----------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    @app.get("/r/{report_id}")
    def report_page(report_id: str) -> FileResponse:
        # Serve the SPA shell; the client loads /api/reports/{id} and renders Report or Not Found cleanly.
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    # ---------- health, version & status ----------

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(response: Response) -> dict[str, Any]:
        st = get_state()
        cur = st.settings
        db_info = st.store.health()
        db_state = db_info.get("status", "failed")

        writable = "ok"
        try:
            st.store.db_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=st.store.db_path.parent, delete=True):
                pass
        except OSError:
            writable = "failed"

        cache_state = "disabled" if not cur.cache_enabled else ("ok" if st.runtime.cache.healthy() else "degraded")
        providers_state = (
            "degraded"
            if (st.runtime.llm_breaker.state == "open" or st.runtime.search_breaker.state == "open")
            else "ok"
        )
        config_state = "ok" if (cur.fake or not cur.missing_keys()) else "degraded"

        components = {
            "database": db_state,
            "storage": writable,
            "cache": cache_state,
            "providers": providers_state,
            "config": config_state,
        }
        if db_state == "failed" or writable == "failed" or (cur.is_production and config_state != "ok"):
            response.status_code = 503
            overall = "not_ready"
        elif any(v == "degraded" for v in components.values()):
            overall = "degraded"
        else:
            overall = "ready"
        return {"status": overall, "components": components}

    @app.get("/version")
    def version() -> dict[str, str]:
        cur = get_state().settings
        return {"version": VERSION, "env": cur.env}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        cur = get_state().settings
        available = bool(cur.fake or not cur.missing_keys())
        out: dict[str, Any] = {
            "available": available,
            "demo_mode": bool(cur.fake),
            "demo_controls": not cur.is_production,
            "setup_enabled": not cur.is_production,
            "needs_onboarding": (not cur.is_production) and (not available),
            "primary_model": cur.primary_model,
            "verifier_model": cur.verifier_model,
        }
        if not cur.is_production:
            out["fake"] = cur.fake
        return out

    # ---------- local onboarding & settings wizard ----------

    @app.get("/api/setup")
    def get_setup(request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        _require_local_setup_allowed(request, st.settings, mutate=False)
        response.headers["Cache-Control"] = "no-store"
        return setup_payload(st.settings, st.env_path)

    @app.post("/api/setup")
    def save_setup(body: SetupUpdateRequest, request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        _require_local_setup_allowed(request, st.settings, mutate=True)
        response.headers["Cache-Control"] = "no-store"
        raw = body.model_dump(exclude_unset=True)
        try:
            new_settings, restart_required = apply_setup_update(st.settings, raw)
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            write_env_file(new_settings, st.env_path)
            sync_process_env(new_settings)
            st.reload_settings(new_settings)
        except OSError as exc:
            ref = capture_error(exc, layer="setup")
            raise HTTPException(500, f"Couldn't write configuration file (ref {ref}).") from None
        log_event(
            logger,
            logging.INFO,
            "setup.updated",
            fake=new_settings.fake,
            primary_model=new_settings.primary_model,
            verifier_model=new_settings.verifier_model,
            restart_required=restart_required,
        )
        out = setup_payload(new_settings, st.env_path)
        out["saved"] = True
        out["restart_required"] = restart_required
        return out

    @app.post("/api/setup/test")
    def test_setup_keys(body: SetupTestRequest, request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        cur = st.settings
        _require_local_setup_allowed(request, cur, mutate=True)
        response.headers["Cache-Control"] = "no-store"
        neb_key = (body.nebius_api_key or "").strip() or cur.nebius_api_key
        tav_key = (body.tavily_api_key or "").strip() or cur.tavily_api_key
        base_url = (body.nebius_base_url or "").strip() or cur.nebius_base_url
        primary = (body.primary_model or "").strip() or cur.primary_model
        verifier = (body.verifier_model or "").strip() or cur.verifier_model
        return verify_connections(
            nebius_api_key=neb_key,
            tavily_api_key=tav_key,
            nebius_base_url=base_url,
            primary_model=primary,
            verifier_model=verifier,
        )

    # ---------- retired GET /api/run ----------

    @app.get("/api/run")
    def retired_get_run() -> JSONResponse:
        return JSONResponse(
            status_code=405,
            headers={"Allow": "POST"},
            content={"detail": "Use POST /api/checks to start a check, then stream GET /api/checks/{id}/events."},
        )

    # ---------- start a check (POST /api/checks) ----------

    @app.post("/api/checks", status_code=202)
    def create_check(body: CreateCheckRequest, request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        cur = st.settings
        _enforce_same_origin(request, cur)
        if st.shutting_down:
            raise HTTPException(503, "Prior Art is restarting. Please try again in a moment.")

        sim = (body.simulate or "").strip()
        if sim:
            if cur.is_production:
                raise HTTPException(400, "Simulation modes are disabled in production.")
            if sim not in SIMULATE_MODES:
                raise HTTPException(400, f"simulate must be one of {SIMULATE_MODES}")

        owner_id, owner_ref = resolve_owner(request, response, create=True)
        assert owner_id is not None
        owner_ref_var.set(owner_ref)

        idem_key = request.headers.get("idempotency-key", "").strip()[:128] or None
        idea_key = normalize_idea_key(body.idea)

        # Attach to an identical in-flight check by the same owner without starting duplicate paid work.
        with st.checks_lock:
            for active in st.active_checks.values():
                if not active.done and active.owner_id == owner_id and active.idea_key == idea_key and not sim:
                    return {"id": active.report_id, "status": "running", "deduplicated": True}

        # Check idempotency key BEFORE consuming rate limits or semaphore.
        if idem_key:
            with st.store.pool.read() as conn:
                existing = conn.execute(
                    "SELECT id, status FROM reports WHERE owner_id = ? AND client_request_id = ?",
                    (owner_id, idem_key),
                ).fetchone()
            if existing is not None:
                return {"id": str(existing[0]), "status": str(existing[1]), "idempotent": True}

        # Daily spend budget guard (skip when demo/fake mode is active).
        if not cur.fake:
            within_budget, _ = st.store.check_daily_budget(
                max_credits=cur.daily_budget_credits,
                max_output_tokens=cur.daily_budget_output_tokens,
            )
            if not within_budget:
                raise HTTPException(
                    503,
                    "Prior Art has reached today's usage limit. It resets at 00:00 UTC.",
                )

        # Persistent SQLite token-bucket rate limits (per-owner 10m & day, per-IP 10m, global hourly).
        ip_hash = _daily_ip_hash(_client_ip(request, cur))
        checks = [
            (f"owner10m:{owner_id}", cur.rate_limit_10m, 600.0, "Too many checks right now. Try again in a few minutes."),
            (
                f"ownerDay:{owner_id}",
                cur.rate_limit_day,
                86400.0,
                f"You've reached today's limit of {cur.rate_limit_day} checks. It resets at 00:00 UTC.",
            ),
            (
                f"ip10m:{ip_hash}",
                max(cur.rate_limit_10m * 2, 6),
                600.0,
                "Too many checks from this network. Try again in a few minutes.",
            ),
            ("globalHr", cur.rate_limit_global_hour, 3600.0, "Prior Art is busy right now. Try again in a minute."),
        ]
        for bkey, cap, window, msg in checks:
            allowed, retry_after = st.store.consume_rate_limit(bkey, capacity=cap, refill_seconds=window)
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail=msg,
                    headers={"Retry-After": str(retry_after)},
                )

        # Global concurrency cap (capture local references so live reload cannot mismatch release/runtime).
        sem = st.semaphore
        rt = st.runtime
        if not sem.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail="Prior Art is busy right now. Try again in a minute.",
                headers={"Retry-After": "15"},
            )

        run_settings = Settings(
            env=cur.env,
            nebius_api_key=cur.nebius_api_key,
            nebius_base_url=cur.nebius_base_url,
            tavily_api_key=cur.tavily_api_key,
            primary_model=cur.primary_model,
            verifier_model=cur.verifier_model,
            max_candidates=cur.max_candidates,
            results_per_query=cur.results_per_query,
            page_chars=cur.page_chars,
            workers=cur.workers,
            search_concurrency=cur.search_concurrency,
            model_concurrency=cur.model_concurrency,
            check_deadline_s=cur.check_deadline_s,
            cache_enabled=cur.cache_enabled,
            cache_path=cur.cache_path,
            db_path=cur.db_path,
            runs_dir=cur.runs_dir,
            # Simulated outage demos use offline demo clients so they never spend real credits.
            fake=True if (sim and not cur.is_production) else cur.fake,
            simulate=sim,
        )

        writer_model = "fake/primary-model" if run_settings.fake else run_settings.primary_model
        verifier_model = "fake/verifier-model" if run_settings.fake else run_settings.verifier_model

        try:
            report_id, created = st.store.create_running_report(
                owner_id=owner_id,
                idea=body.idea,
                writer_model=writer_model,
                verifier_model=verifier_model,
                simulate=sim or None,
                client_request_id=idem_key,
                pivot_of_report_id=body.pivot_of_report_id,
            )
        except Exception:
            sem.release()
            raise

        if not created:
            sem.release()
            return {"id": report_id, "status": "running", "idempotent": True}

        active = _ActiveCheck(report_id=report_id, owner_id=owner_id, idea_key=idea_key)
        with st.checks_lock:
            st.active_checks[report_id] = active

        def on_usage(rec: UsageRecord) -> None:
            st.store.record_usage(rec, check_id=report_id, owner_ref=owner_ref, ip_ref=ip_hash)

        budget = CheckBudget(on_usage=on_usage)

        def emit(kind: str, data: dict[str, Any]) -> None:
            # Delay publishing 'done' until DB commit succeeds so reconnecting readers always see committed data.
            if kind == "done":
                return
            seq = active.publish(kind, data)
            try:
                st.store.append_event(report_id, owner_id, seq, kind, data)
            except Exception as exc:
                log_event(logger, logging.WARNING, "event.persist_failed", check_id=report_id, error=type(exc).__name__)

        def worker() -> None:
            tok_cid = check_id_var.set(report_id)
            tok_own = owner_ref_var.set(owner_ref)
            try:
                agent = build(run_settings, emit=emit, runtime=rt, budget=budget)
                result = agent.run(body.idea, report_id=report_id, save_file=False)
                st.store.finish_report(
                    report_id,
                    owner_id,
                    result,
                    candidates=agent._last_candidates,
                )
                public_result = {k: v for k, v in result.items() if k != "saved_to"}
                seq = active.publish("done", public_result)
                try:
                    st.store.append_event(report_id, owner_id, seq, "done", {"status": result["status"]})
                except Exception:
                    pass
            except Exception as exc:
                ref = capture_error(exc, layer="worker", check_id=report_id)
                friendly = _friendly_error_message(exc, ref)
                err_payload = {"message": friendly, "ref": ref}
                seq = active.publish("check_error", err_payload)
                try:
                    st.store.append_event(report_id, owner_id, seq, "error", err_payload)
                except Exception:
                    pass
                try:
                    st.store.fail_running_report(report_id, owner_id, friendly)
                except Exception:
                    pass
            finally:
                active.finish()
                sem.release()
                check_id_var.reset(tok_cid)
                owner_ref_var.reset(tok_own)
                # Keep finished entry briefly so immediate SSE reconnects see done=True; prune old ones.
                with st.checks_lock:
                    cutoff = time.monotonic() - 300.0
                    stale = [k for k, v in st.active_checks.items() if v.done and v.started_at < cutoff]
                    for k in stale:
                        st.active_checks.pop(k, None)

        th = threading.Thread(target=propagate_context(worker), name=f"check-{report_id}", daemon=False)
        active.thread = th
        th.start()
        return {"id": report_id, "status": "running"}

    # ---------- SSE stream with replay & 15s heartbeat ----------

    @app.get("/api/checks/{report_id}/events")
    async def check_events(
        report_id: str,
        request: Request,
        response: Response,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        rid = _validate_report_id(report_id)
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "Report not found.")

        owner_status = st.store.get_report_owner(rid)
        if owner_status is None or owner_status[0] != owner_id:
            raise HTTPException(404, "Report not found.")

        last_event_id = request.headers.get("last-event-id", "").strip()
        if last_event_id.isdigit():
            after = max(after, int(last_event_id))

        async def stream() -> AsyncIterator[str]:
            yield "retry: 5000\n\n"
            # Subscribe before replaying so no live event is missed in the gap.
            with st.checks_lock:
                active = st.active_checks.get(rid)
            q: asyncio.Queue[tuple[int, str, dict[str, Any]] | None] | None = None
            already_done = True
            if active is not None:
                q, already_done = active.subscribe()

            last_sent = after
            terminal_sent = False
            try:
                history = st.store.events_after(rid, owner_id, after_seq=after) or []
                for ev in history:
                    seq = int(ev["seq"])
                    if seq <= last_sent:
                        continue
                    last_sent = seq
                    if ev["kind"] == "done":
                        rep = st.store.get_report(rid, owner_id)
                        if rep is not None:
                            yield _sse_frame(seq, "done", rep)
                            terminal_sent = True
                            return
                    else:
                        sse_kind = "check_error" if ev["kind"] == "error" else ev["kind"]
                        yield _sse_frame(seq, sse_kind, ev["data"])
                        if sse_kind in ("error", "check_error"):
                            terminal_sent = True
                            return

                if active is None or already_done:
                    rep = st.store.get_report(rid, owner_id)
                    if rep is not None and rep["status"] != "running" and not terminal_sent:
                        yield _sse_frame(last_sent + 1, "done", rep)
                    return

                assert q is not None
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        if not terminal_sent:
                            rep = st.store.get_report(rid, owner_id)
                            if rep is not None and rep["status"] != "running":
                                yield _sse_frame(last_sent + 1, "done", rep)
                        return
                    seq, kind, data = item
                    if seq <= last_sent:
                        continue
                    last_sent = seq
                    yield _sse_frame(seq, kind, data)
                    if kind in ("done", "check_error", "error"):
                        terminal_sent = True
                        return
            finally:
                if active is not None and q is not None:
                    active.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Content-Encoding": "identity",
            },
        )

    # ---------- owner-scoped report reads, exports & lifecycle ----------

    @app.get("/api/reports")
    @app.get("/api/runs")
    def list_reports(request: Request, response: Response, limit: int = Query(default=30, ge=1, le=100)) -> list[dict[str, Any]]:
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            return []
        return st.store.list_reports(owner_id, limit=limit)

    @app.get("/api/reports/deleted")
    def list_deleted_reports(request: Request, response: Response) -> list[dict[str, Any]]:
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            return []
        return st.store.list_deleted_reports(owner_id)

    @app.get("/api/reports/{report_id}")
    @app.get("/api/runs/{report_id}")
    def get_report(report_id: str, request: Request, response: Response) -> dict[str, Any]:
        rid = _validate_report_id(report_id)
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "Report not found.")
        rep = st.store.get_report(rid, owner_id)
        if rep is None:
            raise HTTPException(404, "Report not found.")
        return rep

    @app.get("/api/reports/{report_id}/report.md")
    @app.get("/api/report/{report_id}")
    @app.get("/r/{report_id}/report.md")
    def download_report_markdown(report_id: str, request: Request, response: Response) -> PlainTextResponse:
        rid = _validate_report_id(report_id)
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "Report not found.")
        rep = st.store.get_report(rid, owner_id)
        if rep is None:
            raise HTTPException(404, "Report not found.")
        md = to_markdown(rep)
        fname = f"prior-art-report-{rid}.md"
        return PlainTextResponse(
            md,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"; filename*=UTF-8\'\'{fname}',
                "Cache-Control": "private, no-cache",
            },
        )

    @app.delete("/api/reports/{report_id}")
    def delete_report(report_id: str, request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        _enforce_same_origin(request, st.settings)
        rid = _validate_report_id(report_id)
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "Report not found.")
        try:
            ok = st.store.delete_report(rid, owner_id)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Wait for the check to finish before deleting it.") from None
        if not ok:
            raise HTTPException(404, "Report not found.")
        return {"deleted": True, "id": rid}

    @app.post("/api/reports/{report_id}/restore")
    def restore_report(report_id: str, request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        _enforce_same_origin(request, st.settings)
        rid = _validate_report_id(report_id)
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "Report not found.")
        ok = st.store.restore_report(rid, owner_id)
        if not ok:
            raise HTTPException(404, "Report cannot be restored (not found or past 30-day window).")
        return {"restored": True, "id": rid}

    @app.get("/api/me/export")
    def export_me(request: Request, response: Response) -> JSONResponse:
        st = get_state()
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "No data stored for this browser.")
        data = st.store.export_owner(owner_id)
        return JSONResponse(
            content=data,
            headers={
                "Content-Disposition": 'attachment; filename="prior-art-my-data.json"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.delete("/api/me")
    def delete_me(request: Request, response: Response) -> dict[str, Any]:
        st = get_state()
        _enforce_same_origin(request, st.settings)
        owner_id, _ = resolve_owner(request, response, create=False)
        if owner_id is None:
            raise HTTPException(404, "No data stored for this browser.")
        counts = st.store.delete_owner(owner_id)
        response.delete_cookie("__Host-pa_owner", path="/")
        response.delete_cookie("pa_owner", path="/")
        return {"deleted": True, "counts": counts}

    return app


app = create_app(Settings.from_env(validate=False))

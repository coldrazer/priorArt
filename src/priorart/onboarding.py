"""Onboarding & settings wizard backend: .env persistence, validation, live key checks, and CLI wizard."""
from __future__ import annotations

import builtins
import getpass
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from .config import ROOT, ConfigError, Settings
from .log import scrub_text

RECOMMENDED_WRITER_MODELS: tuple[str, ...] = (
    "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct",
    "deepseek-ai/DeepSeek-V3",
    "mistralai/Mistral-Large-Instruct-2411",
)

RECOMMENDED_VERIFIER_MODELS: tuple[str, ...] = (
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen2.5-72B-Instruct",
    "deepseek-ai/DeepSeek-R1",
    "meta-llama/Llama-3.3-70B-Instruct",
)

BASE_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]", "testserver"})

SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "max_candidates": (1, 12),
    "results_per_query": (1, 10),
    "page_chars": (1_000, 12_000),
    "workers": (1, 16),
    "search_concurrency": (1, 16),
    "model_concurrency": (1, 16),
    "check_deadline_s": (10, 600),
    "max_concurrent_checks": (1, 32),
    "rate_limit_10m": (1, 1_000),
    "rate_limit_day": (1, 10_000),
    "rate_limit_global_hour": (1, 10_000),
    "daily_budget_credits": (1, 10_000_000),
    "daily_budget_output_tokens": (1_000, 1_000_000_000),
}

MANAGED_ENV_KEYS: tuple[str, ...] = (
    "NEBIUS_API_KEY",
    "TAVILY_API_KEY",
    "NEBIUS_BASE_URL",
    "PRIORART_PRIMARY_MODEL",
    "PRIORART_VERIFIER_MODEL",
    "PRIORART_FAKE",
    "PRIORART_MAX_CANDIDATES",
    "PRIORART_RESULTS_PER_QUERY",
    "PRIORART_PAGE_CHARS",
    "PRIORART_WORKERS",
    "PRIORART_SEARCH_CONCURRENCY",
    "PRIORART_MODEL_CONCURRENCY",
    "PRIORART_CHECK_DEADLINE_S",
    "PRIORART_MAX_CONCURRENT_CHECKS",
    "PRIORART_CACHE",
    "PRIORART_CACHE_PATH",
    "PRIORART_DB_PATH",
    "PRIORART_RUNS_DIR",
    "PRIORART_RATE_LIMIT_10M",
    "PRIORART_RATE_LIMIT_DAY",
    "PRIORART_RATE_LIMIT_GLOBAL_HOUR",
    "PRIORART_DAILY_BUDGET_CREDITS",
    "PRIORART_DAILY_BUDGET_OUTPUT_TOKENS",
    "PRIORART_ALLOWED_HOSTS",
    "PRIORART_PUBLIC_ORIGIN",
    "PRIORART_TRUST_PROXY_HEADERS",
)


def is_key_set(value: str) -> bool:
    cleaned = (value or "").strip()
    return bool(cleaned and not cleaned.startswith("your-"))


def mask_secret(value: str) -> str:
    if not is_key_set(value):
        return ""
    cleaned = value.strip()
    return f"***{cleaned[-4:]}" if len(cleaned) > 8 else "***"


def model_family(model_id: str) -> str:
    return (model_id or "").strip().split("/")[0].lower()


def extra_allowed_hosts(settings: Settings) -> list[str]:
    return [h for h in settings.allowed_hosts if h not in BASE_ALLOWED_HOSTS]


def _check_no_control_chars(label: str, value: str) -> None:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ConfigError(f"{label}: cannot contain newline or null characters")


def _validate_http_url(label: str, value: str, *, allow_empty: bool = False) -> str:
    cleaned = value.strip()
    _check_no_control_chars(label, cleaned)
    if not cleaned:
        if allow_empty:
            return ""
        raise ConfigError(f"{label}: must be a valid http:// or https:// URL")
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{label}: must be a valid http:// or https:// URL")
    return cleaned


def setup_payload(settings: Settings, env_path: Path | None = None) -> dict[str, Any]:
    """Build a safe, secret-masked settings snapshot for the onboarding UI."""
    target = env_path or (ROOT / ".env")
    missing = settings.missing_keys()
    extras = extra_allowed_hosts(settings)
    return {
        "env": settings.env,
        "env_file": target.name,
        "env_file_exists": target.exists(),
        "needs_onboarding": (not settings.fake) and bool(missing),
        "missing_keys": missing,
        "nebius_api_key_set": is_key_set(settings.nebius_api_key),
        "nebius_api_key_masked": mask_secret(settings.nebius_api_key),
        "tavily_api_key_set": is_key_set(settings.tavily_api_key),
        "tavily_api_key_masked": mask_secret(settings.tavily_api_key),
        "nebius_base_url": settings.nebius_base_url,
        "primary_model": settings.primary_model,
        "verifier_model": settings.verifier_model,
        "same_model_family": bool(
            model_family(settings.primary_model)
            and model_family(settings.primary_model) == model_family(settings.verifier_model)
        ),
        "fake": settings.fake,
        "max_candidates": settings.max_candidates,
        "results_per_query": settings.results_per_query,
        "page_chars": settings.page_chars,
        "workers": settings.workers,
        "search_concurrency": settings.search_concurrency,
        "model_concurrency": settings.model_concurrency,
        "check_deadline_s": settings.check_deadline_s,
        "max_concurrent_checks": settings.max_concurrent_checks,
        "cache_enabled": settings.cache_enabled,
        "rate_limit_10m": settings.rate_limit_10m,
        "rate_limit_day": settings.rate_limit_day,
        "rate_limit_global_hour": settings.rate_limit_global_hour,
        "daily_budget_credits": settings.daily_budget_credits,
        "daily_budget_output_tokens": settings.daily_budget_output_tokens,
        "allowed_hosts": ", ".join(extras),
        "public_origin": settings.public_origin,
        "trust_proxy_headers": settings.trust_proxy_headers,
        "db_path": settings.db_path,
        "cache_path": settings.cache_path,
        "runs_dir": settings.runs_dir,
        "recommended_writer_models": list(RECOMMENDED_WRITER_MODELS),
        "recommended_verifier_models": list(RECOMMENDED_VERIFIER_MODELS),
        "bounds": {k: {"min": lo, "max": hi} for k, (lo, hi) in SETTING_BOUNDS.items()},
    }


def _coerce_int(name: str, raw: Any, default: int) -> int:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ConfigError(f"{name}: must be an integer")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: must be an integer") from exc


def _coerce_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return bool(raw)


def apply_setup_update(current: Settings, data: dict[str, Any]) -> tuple[Settings, list[str]]:
    """Validate and merge incoming onboarding settings onto `current`.

    Returns `(new_settings, restart_required_fields)`.
    """
    errors: list[str] = []

    def get_str(key: str, default: str) -> str:
        val = data.get(key)
        if val is None:
            return default
        s = str(val).strip()
        try:
            _check_no_control_chars(key, s)
        except ConfigError as exc:
            errors.append(str(exc))
        return s

    # API keys: blank preserves existing unless explicitly cleared.
    if _coerce_bool(data.get("clear_nebius_key"), False):
        nebius_key = ""
    else:
        raw_neb = data.get("nebius_api_key")
        if raw_neb is not None and str(raw_neb).strip() != "":
            nebius_key = get_str("nebius_api_key", "")
        else:
            nebius_key = current.nebius_api_key

    if _coerce_bool(data.get("clear_tavily_key"), False):
        tavily_key = ""
    else:
        raw_tav = data.get("tavily_api_key")
        if raw_tav is not None and str(raw_tav).strip() != "":
            tavily_key = get_str("tavily_api_key", "")
        else:
            tavily_key = current.tavily_api_key

    try:
        nebius_base_url = _validate_http_url(
            "NEBIUS_BASE_URL",
            str(data["nebius_base_url"]) if "nebius_base_url" in data and data["nebius_base_url"] is not None else current.nebius_base_url,
            allow_empty=False,
        )
    except ConfigError as exc:
        errors.append(str(exc))
        nebius_base_url = current.nebius_base_url

    try:
        public_origin = _validate_http_url(
            "PRIORART_PUBLIC_ORIGIN",
            str(data["public_origin"]) if "public_origin" in data and data["public_origin"] is not None else current.public_origin,
            allow_empty=True,
        ).rstrip("/")
    except ConfigError as exc:
        errors.append(str(exc))
        public_origin = current.public_origin

    primary_model = get_str("primary_model", current.primary_model)
    verifier_model = get_str("verifier_model", current.verifier_model)
    db_path = get_str("db_path", current.db_path) or current.db_path
    cache_path = get_str("cache_path", current.cache_path) or current.cache_path
    runs_dir = get_str("runs_dir", current.runs_dir) or current.runs_dir

    # Extra allowed hosts merged onto local defaults.
    base_hosts = ["127.0.0.1", "localhost", "[::1]"]
    if not current.is_production:
        base_hosts.append("testserver")
    if "allowed_hosts" in data and data["allowed_hosts"] is not None:
        raw_hosts = data["allowed_hosts"]
        if isinstance(raw_hosts, list):
            parts = [str(x).strip() for x in raw_hosts if str(x).strip()]
        else:
            host_str = get_str("allowed_hosts", "")
            parts = [h.strip() for h in host_str.split(",") if h.strip()]
        allowed_hosts: list[str] = []
        for h in [*base_hosts, *parts]:
            if h not in allowed_hosts:
                allowed_hosts.append(h)
    else:
        allowed_hosts = list(current.allowed_hosts)

    int_vals: dict[str, int] = {}
    for field_name in SETTING_BOUNDS:
        cur_val = int(getattr(current, field_name))
        try:
            int_vals[field_name] = _coerce_int(field_name, data.get(field_name), cur_val)
        except ConfigError as exc:
            errors.append(str(exc))
            int_vals[field_name] = cur_val

    new_settings = Settings(
        env=current.env,
        nebius_api_key=nebius_key,
        nebius_base_url=nebius_base_url,
        tavily_api_key=tavily_key,
        primary_model=primary_model,
        verifier_model=verifier_model,
        max_candidates=int_vals["max_candidates"],
        results_per_query=int_vals["results_per_query"],
        page_chars=int_vals["page_chars"],
        workers=int_vals["workers"],
        search_concurrency=int_vals["search_concurrency"],
        model_concurrency=int_vals["model_concurrency"],
        check_deadline_s=int_vals["check_deadline_s"],
        max_concurrent_checks=int_vals["max_concurrent_checks"],
        cache_enabled=_coerce_bool(data.get("cache_enabled"), current.cache_enabled),
        cache_path=cache_path,
        db_path=db_path,
        runs_dir=runs_dir,
        fake=_coerce_bool(data.get("fake"), current.fake),
        simulate=current.simulate,
        allowed_hosts=allowed_hosts,
        public_origin=public_origin,
        trust_proxy_headers=_coerce_bool(data.get("trust_proxy_headers"), current.trust_proxy_headers),
        rate_limit_10m=int_vals["rate_limit_10m"],
        rate_limit_day=int_vals["rate_limit_day"],
        rate_limit_global_hour=int_vals["rate_limit_global_hour"],
        daily_budget_credits=int_vals["daily_budget_credits"],
        daily_budget_output_tokens=int_vals["daily_budget_output_tokens"],
    )

    try:
        new_settings.validate(require_keys=False)
    except ConfigError as exc:
        errors.extend(str(exc).splitlines())

    if errors:
        raise ConfigError("\n".join(errors))

    restart_required: list[str] = []
    if new_settings.db_path != current.db_path:
        restart_required.append("db_path")
    if set(new_settings.allowed_hosts) != set(current.allowed_hosts):
        restart_required.append("allowed_hosts")
    return new_settings, restart_required


def format_env_value(value: str) -> str:
    _check_no_control_chars("env value", value)
    if value == "":
        return ""
    needs_quotes = any(ch in value for ch in (" ", "\t", "#", '"', "'", "$"))
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def settings_to_env_map(settings: Settings) -> dict[str, str | None]:
    """Map Settings fields to .env key/value pairs (None removes an unset optional key)."""
    extras = extra_allowed_hosts(settings)
    return {
        "NEBIUS_API_KEY": settings.nebius_api_key if is_key_set(settings.nebius_api_key) else None,
        "TAVILY_API_KEY": settings.tavily_api_key if is_key_set(settings.tavily_api_key) else None,
        "NEBIUS_BASE_URL": settings.nebius_base_url,
        "PRIORART_PRIMARY_MODEL": settings.primary_model,
        "PRIORART_VERIFIER_MODEL": settings.verifier_model,
        "PRIORART_FAKE": "1" if settings.fake else "0",
        "PRIORART_MAX_CANDIDATES": str(settings.max_candidates),
        "PRIORART_RESULTS_PER_QUERY": str(settings.results_per_query),
        "PRIORART_PAGE_CHARS": str(settings.page_chars),
        "PRIORART_WORKERS": str(settings.workers),
        "PRIORART_SEARCH_CONCURRENCY": str(settings.search_concurrency),
        "PRIORART_MODEL_CONCURRENCY": str(settings.model_concurrency),
        "PRIORART_CHECK_DEADLINE_S": str(settings.check_deadline_s),
        "PRIORART_MAX_CONCURRENT_CHECKS": str(settings.max_concurrent_checks),
        "PRIORART_CACHE": "1" if settings.cache_enabled else "0",
        "PRIORART_CACHE_PATH": settings.cache_path,
        "PRIORART_DB_PATH": settings.db_path,
        "PRIORART_RUNS_DIR": settings.runs_dir,
        "PRIORART_RATE_LIMIT_10M": str(settings.rate_limit_10m),
        "PRIORART_RATE_LIMIT_DAY": str(settings.rate_limit_day),
        "PRIORART_RATE_LIMIT_GLOBAL_HOUR": str(settings.rate_limit_global_hour),
        "PRIORART_DAILY_BUDGET_CREDITS": str(settings.daily_budget_credits),
        "PRIORART_DAILY_BUDGET_OUTPUT_TOKENS": str(settings.daily_budget_output_tokens),
        "PRIORART_ALLOWED_HOSTS": ",".join(extras) if extras else None,
        "PRIORART_PUBLIC_ORIGIN": settings.public_origin or None,
        "PRIORART_TRUST_PROXY_HEADERS": "1" if settings.trust_proxy_headers else "0",
    }


def write_env_file(settings: Settings, env_path: Path | None = None) -> Path:
    """Atomically write settings into `.env` with owner-only `0600` permissions.

    Preserves unrelated comments and custom keys already present in the file.
    """
    target = (env_path or (ROOT / ".env")).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    env_map = settings_to_env_map(settings)
    managed_set = set(MANAGED_ENV_KEYS)

    existing_lines: list[str] = []
    if target.exists():
        existing_lines = target.read_text(encoding="utf-8").splitlines()

    written_keys: set[str] = set()
    output_lines: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue
        raw_key = stripped.split("=", 1)[0].strip()
        key = raw_key[7:].strip() if raw_key.startswith("export ") else raw_key
        if key not in managed_set:
            output_lines.append(line)
            continue
        if key in written_keys:
            continue
        val = env_map.get(key)
        written_keys.add(key)
        if val is not None:
            output_lines.append(f"{key}={format_env_value(val)}")

    remaining = [k for k in MANAGED_ENV_KEYS if k not in written_keys and env_map.get(k) is not None]
    if remaining:
        if output_lines and output_lines[-1].strip() != "":
            output_lines.append("")
        if not existing_lines:
            output_lines.append("# Prior Art configuration (generated by onboarding wizard — never commit .env)")
        for key in remaining:
            val = env_map[key]
            assert val is not None
            output_lines.append(f"{key}={format_env_value(val)}")

    payload = "\n".join(output_lines).rstrip("\n") + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".env.tmp.", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def sync_process_env(settings: Settings) -> None:
    """Keep os.environ aligned with updated Settings for in-process callers."""
    for key, val in settings_to_env_map(settings).items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def verify_connections(
    *,
    nebius_api_key: str,
    tavily_api_key: str,
    nebius_base_url: str,
    primary_model: str = "",
    verifier_model: str = "",
) -> dict[str, Any]:
    """Verify Nebius Token Factory and Tavily API keys live without exposing secrets."""
    from .cache import Cache
    from .llm import LLM

    try:
        base_url = _validate_http_url("NEBIUS_BASE_URL", nebius_base_url, allow_empty=False)
    except ConfigError as exc:
        return {
            "nebius": {"ok": False, "tested": False, "message": str(exc), "models": []},
            "tavily": {"ok": False, "tested": False, "message": "Skipped due to invalid base URL."},
        }

    def _safe_err(exc: Exception) -> str:
        msg = scrub_text(str(exc))
        for raw_key in (nebius_api_key.strip(), tavily_api_key.strip()):
            if raw_key and len(raw_key) > 4:
                msg = msg.replace(raw_key, "[REDACTED]")
        return msg[:220]

    nebius_res: dict[str, Any]
    if not is_key_set(nebius_api_key):
        nebius_res = {"ok": False, "tested": False, "message": "No Nebius API key provided.", "models": []}
    else:
        probe_settings = Settings(
            nebius_api_key=nebius_api_key.strip(),
            nebius_base_url=base_url,
            tavily_api_key="placeholder",
            primary_model=primary_model or "meta-llama/Llama-3.3-70B-Instruct",
            verifier_model=verifier_model or "Qwen/Qwen3-235B-A22B-Instruct-2507",
            cache_enabled=False,
        )
        llm = LLM(probe_settings, Cache("", enabled=False))
        try:
            models = llm.list_models()
            nebius_res = {
                "ok": True,
                "tested": True,
                "message": f"Connected — {len(models)} models available.",
                "models": models[:120],
                "primary_available": (primary_model in models) if primary_model else None,
                "verifier_available": (verifier_model in models) if verifier_model else None,
            }
        except Exception as exc:
            nebius_res = {
                "ok": False,
                "tested": True,
                "message": f"Connection failed: {_safe_err(exc)}",
                "models": [],
            }
        finally:
            llm.close()

    tavily_res: dict[str, Any]
    if not is_key_set(tavily_api_key):
        tavily_res = {"ok": False, "tested": False, "message": "No Tavily API key provided."}
    else:
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=tavily_api_key.strip())
            resp = client.search(query="prior art test", search_depth="basic", max_results=1, timeout=12)
            count = len(resp.get("results", [])) if isinstance(resp, dict) else 0
            tavily_res = {
                "ok": True,
                "tested": True,
                "message": f"Connected — search responded cleanly ({count} result{'s' if count != 1 else ''}).",
            }
        except Exception as exc:
            tavily_res = {
                "ok": False,
                "tested": True,
                "message": f"Connection failed: {_safe_err(exc)}",
            }

    return {"nebius": nebius_res, "tavily": tavily_res}


# ---------- Interactive CLI Onboarding Wizard ----------


def _ask_str(prompt: str, default: str, input_fn: Callable[[str], str]) -> str:
    shown = f" [{default}]" if default else ""
    raw = input_fn(f"{prompt}{shown}: ").strip()
    return raw if raw else default


def _ask_bool(prompt: str, default: bool, input_fn: Callable[[str], str]) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input_fn(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False


def _ask_int(prompt: str, default: int, lo: int, hi: int, input_fn: Callable[[str], str], out: TextIO) -> int:
    while True:
        raw = input_fn(f"{prompt} ({lo}–{hi}) [{default}]: ").strip()
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError:
            print(f"  Please enter an integer between {lo} and {hi}.", file=out)
            continue
        if lo <= val <= hi:
            return val
        print(f"  Value must be between {lo} and {hi}.", file=out)


def run_cli_wizard(
    env_path: Path | None = None,
    *,
    input_fn: Callable[[str], str] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Interactive terminal onboarding wizard for configuring API keys and all settings."""
    in_fn = input_fn if input_fn is not None else builtins.input
    gp_fn = getpass_fn if getpass_fn is not None else getpass.getpass
    stream = out if out is not None else sys.stdout
    target = (env_path or (ROOT / ".env")).expanduser()
    current = Settings.from_env(validate=False)

    print("\n" + "=" * 72, file=stream)
    print("  Prior Art — Onboarding & Configuration Wizard", file=stream)
    print("=" * 72, file=stream)
    print(f"Target config file: {target} (saved with 0600 permissions; git-ignored)\n", file=stream)

    # Step 1: Mode & API Keys
    print("Step 1/4 · Mode & API Keys", file=stream)
    print("-" * 40, file=stream)
    fake_mode = _ask_bool("Use offline demo mode (no API keys required)?", current.fake, in_fn)

    nebius_key = current.nebius_api_key
    tavily_key = current.tavily_api_key
    nebius_base_url = current.nebius_base_url

    if not fake_mode:
        neb_hint = f"keep {mask_secret(nebius_key)}" if is_key_set(nebius_key) else "required"
        entered_neb = gp_fn(f"Nebius Token Factory API key ({neb_hint}; Enter to keep): ").strip()
        if entered_neb:
            nebius_key = entered_neb

        tav_hint = f"keep {mask_secret(tavily_key)}" if is_key_set(tavily_key) else "required"
        entered_tav = gp_fn(f"Tavily Search API key ({tav_hint}; Enter to keep): ").strip()
        if entered_tav:
            tavily_key = entered_tav

        nebius_base_url = _ask_str("Nebius OpenAI-compatible Base URL", nebius_base_url, in_fn)

    discovered_models: list[str] = []
    if not fake_mode and (is_key_set(nebius_key) or is_key_set(tavily_key)):
        if _ask_bool("Test API key connections now?", False, in_fn):
            print("  Testing connections...", file=stream)
            res = verify_connections(
                nebius_api_key=nebius_key,
                tavily_api_key=tavily_key,
                nebius_base_url=nebius_base_url,
                primary_model=current.primary_model,
                verifier_model=current.verifier_model,
            )
            print(f"  Nebius: {'✓' if res['nebius']['ok'] else '✗'} {res['nebius']['message']}", file=stream)
            print(f"  Tavily: {'✓' if res['tavily']['ok'] else '✗'} {res['tavily']['message']}", file=stream)
            discovered_models = list(res["nebius"].get("models") or [])

    # Step 2: Models
    print("\nStep 2/4 · Cross-Family Models", file=stream)
    print("-" * 40, file=stream)
    if discovered_models:
        print(f"  Sample available models: {', '.join(discovered_models[:6])}", file=stream)
    primary_model = _ask_str("Writer model (PRIORART_PRIMARY_MODEL)", current.primary_model, in_fn)
    verifier_model = _ask_str("Verifier model (PRIORART_VERIFIER_MODEL)", current.verifier_model, in_fn)
    if model_family(primary_model) == model_family(verifier_model):
        print(
            "  Note: writer and verifier share the same model family. A distinct family catches more errors.",
            file=stream,
        )

    updates: dict[str, Any] = {
        "fake": fake_mode,
        "nebius_api_key": nebius_key,
        "tavily_api_key": tavily_key,
        "nebius_base_url": nebius_base_url,
        "primary_model": primary_model,
        "verifier_model": verifier_model,
    }

    # Step 3: Pipeline tuning (optional)
    print("\nStep 3/4 · Search, Concurrency & Caching", file=stream)
    print("-" * 40, file=stream)
    if _ask_bool("Customize search/pipeline performance settings?", False, in_fn):
        for key, label in (
            ("max_candidates", "Max projects compared per check"),
            ("results_per_query", "Tavily results per query"),
            ("page_chars", "Max characters read per page"),
            ("workers", "Worker threads"),
            ("search_concurrency", "Parallel search concurrency"),
            ("model_concurrency", "Parallel LLM concurrency"),
            ("check_deadline_s", "Per-check wall-clock deadline (seconds)"),
            ("max_concurrent_checks", "Max concurrent server checks"),
        ):
            lo, hi = SETTING_BOUNDS[key]
            updates[key] = _ask_int(label, int(getattr(current, key)), lo, hi, in_fn, stream)
        updates["cache_enabled"] = _ask_bool("Enable SQLite API cache (24h TTL)?", current.cache_enabled, in_fn)

    # Step 4: Safety, rate limits, budgets & storage (optional)
    print("\nStep 4/4 · Rate Limits, Spend Budgets & Storage", file=stream)
    print("-" * 40, file=stream)
    if _ask_bool("Customize rate limits, daily spend caps, hosts, or storage paths?", False, in_fn):
        for key, label in (
            ("rate_limit_10m", "Per-owner checks per 10 minutes"),
            ("rate_limit_day", "Per-owner checks per day"),
            ("rate_limit_global_hour", "Global server checks per hour"),
            ("daily_budget_credits", "Daily Tavily credit ceiling"),
            ("daily_budget_output_tokens", "Daily LLM output token ceiling"),
        ):
            lo, hi = SETTING_BOUNDS[key]
            updates[key] = _ask_int(label, int(getattr(current, key)), lo, hi, in_fn, stream)
        updates["allowed_hosts"] = _ask_str("Extra allowed Host headers (comma-separated)", ", ".join(extra_allowed_hosts(current)), in_fn)
        updates["public_origin"] = _ask_str("Public origin URL (optional, e.g. https://prior-art.example)", current.public_origin, in_fn)
        updates["trust_proxy_headers"] = _ask_bool("Trust X-Forwarded-* proxy headers?", current.trust_proxy_headers, in_fn)
        updates["db_path"] = _ask_str("SQLite database path", current.db_path, in_fn)
        updates["cache_path"] = _ask_str("SQLite cache path", current.cache_path, in_fn)
        updates["runs_dir"] = _ask_str("CLI runs output directory", current.runs_dir, in_fn)

    try:
        new_settings, _ = apply_setup_update(current, updates)
    except ConfigError as exc:
        print(f"\nConfiguration error:\n{exc}", file=stream)
        return 2

    print("\nSummary (secrets masked):", file=stream)
    print(f"  Mode          : {'Offline demo (PRIORART_FAKE=1)' if new_settings.fake else 'Live APIs'}", file=stream)
    print(f"  Nebius key    : {mask_secret(new_settings.nebius_api_key) or '<unset>'}", file=stream)
    print(f"  Tavily key    : {mask_secret(new_settings.tavily_api_key) or '<unset>'}", file=stream)
    print(f"  Writer model  : {new_settings.primary_model}", file=stream)
    print(f"  Verifier model: {new_settings.verifier_model}", file=stream)
    print(
        f"  Budgets       : {new_settings.max_candidates} projects · {new_settings.check_deadline_s}s deadline · "
        f"{new_settings.daily_budget_credits} credits/day",
        file=stream,
    )

    if not _ask_bool(f"\nSave settings to {target}?", True, in_fn):
        print("Cancelled — no files were modified.", file=stream)
        return 0

    write_env_file(new_settings, target)
    sync_process_env(new_settings)
    print(f"\n✓ Saved configuration to {target} (mode 0600).", file=stream)
    if new_settings.missing_keys():
        print(f"  Note: still missing {', '.join(new_settings.missing_keys())} for live checks.", file=stream)
    print("  Next: run `python src/agent.py serve` or `python src/agent.py \"Your idea\"`.\n", file=stream)
    return 0

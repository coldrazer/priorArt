"""Settings, read from environment variables (or a .env file in non-production)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]  # repo root (src/priorart/config.py -> repo)
VERSION = "0.2.0"
SIMULATE_MODES = ("search_down", "extract_down")
VALID_ENVS = ("development", "staging", "production")

_DOTENV_LOADED = False


def _load_dotenv_if_allowed() -> None:
    """Load .env from the repo root outside production (never overrides real env vars)."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    if os.getenv("PRIORART_ENV", "development").strip().lower() == "production":
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    _DOTENV_LOADED = True


_load_dotenv_if_allowed()


class ConfigError(ValueError):
    """Raised when configuration values are missing, malformed, or out of bounds."""


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    low = v.strip().lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}={v!r}: must be a boolean (1/0, true/false, yes/no)")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v.strip())
    except ValueError as e:
        raise ConfigError(f"{name}={v!r}: must be an integer") from e


def _default_allowed_hosts() -> list[str]:
    raw = os.getenv("PRIORART_ALLOWED_HOSTS", "").strip()
    base = ["127.0.0.1", "localhost", "[::1]"]
    env = os.getenv("PRIORART_ENV", "development").strip().lower()
    if env != "production":
        base.append("testserver")
    if not raw:
        return base
    extras = [h.strip() for h in raw.split(",") if h.strip()]
    seen: list[str] = []
    for h in [*base, *extras]:
        if h not in seen:
            seen.append(h)
    return seen


def _is_unsafe_dir(path_str: str) -> bool:
    if not path_str or path_str == ":memory:":
        return False
    try:
        resolved = Path(path_str).expanduser().resolve()
    except OSError:
        return True
    if resolved == resolved.parent:  # filesystem root ("/")
        return True
    try:
        if resolved == Path.home().resolve():
            return True
    except RuntimeError:
        pass
    return False


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.getenv("PRIORART_ENV", "development").strip().lower() or "development")
    nebius_api_key: str = field(default_factory=lambda: os.getenv("NEBIUS_API_KEY", ""))
    nebius_base_url: str = field(
        default_factory=lambda: os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
    )
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))

    # Two different model families on purpose: the verifier should not share the writer's blind spots.
    # Run `python -m priorart models` to see what your key can use, then set these in .env.
    primary_model: str = field(
        default_factory=lambda: os.getenv("PRIORART_PRIMARY_MODEL")
        or os.getenv("NEBIUS_MODEL")
        or "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    )
    verifier_model: str = field(
        default_factory=lambda: os.getenv("PRIORART_VERIFIER_MODEL", "Qwen/Qwen3-235B-A22B-Instruct-2507")
    )

    max_candidates: int = field(default_factory=lambda: _int("PRIORART_MAX_CANDIDATES", 8))
    results_per_query: int = field(default_factory=lambda: _int("PRIORART_RESULTS_PER_QUERY", 5))
    page_chars: int = field(default_factory=lambda: _int("PRIORART_PAGE_CHARS", 9000))
    workers: int = field(default_factory=lambda: _int("PRIORART_WORKERS", 6))
    search_concurrency: int = field(default_factory=lambda: _int("PRIORART_SEARCH_CONCURRENCY", 14))
    model_concurrency: int = field(default_factory=lambda: _int("PRIORART_MODEL_CONCURRENCY", 8))
    check_deadline_s: int = field(default_factory=lambda: _int("PRIORART_CHECK_DEADLINE_S", 240))
    max_concurrent_checks: int = field(default_factory=lambda: _int("PRIORART_MAX_CONCURRENT_CHECKS", 4))

    cache_enabled: bool = field(default_factory=lambda: _bool("PRIORART_CACHE", True))
    cache_path: str = field(
        default_factory=lambda: os.getenv("PRIORART_CACHE_PATH", str(ROOT / ".cache" / "priorart.sqlite"))
    )
    db_path: str = field(
        default_factory=lambda: os.getenv("PRIORART_DB_PATH", str(ROOT / "data" / "priorart.sqlite"))
    )
    runs_dir: str = field(
        default_factory=lambda: os.getenv("PRIORART_RUNS_DIR", str(ROOT / "runs"))
    )
    fake: bool = field(default_factory=lambda: _bool("PRIORART_FAKE", False))
    simulate: str = field(default_factory=lambda: os.getenv("PRIORART_SIMULATE", "").strip())

    # Security & abuse guardrails
    allowed_hosts: list[str] = field(default_factory=_default_allowed_hosts)
    public_origin: str = field(default_factory=lambda: os.getenv("PRIORART_PUBLIC_ORIGIN", "").strip().rstrip("/"))
    trust_proxy_headers: bool = field(default_factory=lambda: _bool("PRIORART_TRUST_PROXY_HEADERS", False))
    rate_limit_10m: int = field(default_factory=lambda: _int("PRIORART_RATE_LIMIT_10M", 3))
    rate_limit_day: int = field(default_factory=lambda: _int("PRIORART_RATE_LIMIT_DAY", 15))
    rate_limit_global_hour: int = field(default_factory=lambda: _int("PRIORART_RATE_LIMIT_GLOBAL_HOUR", 60))
    daily_budget_credits: int = field(default_factory=lambda: _int("PRIORART_DAILY_BUDGET_CREDITS", 5000))
    daily_budget_output_tokens: int = field(
        default_factory=lambda: _int("PRIORART_DAILY_BUDGET_OUTPUT_TOKENS", 2_000_000)
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def missing_keys(self) -> list[str]:
        if self.fake:
            return []
        out = []
        if not self.nebius_api_key or self.nebius_api_key.startswith("your-"):
            out.append("NEBIUS_API_KEY")
        if not self.tavily_api_key or self.tavily_api_key.startswith("your-"):
            out.append("TAVILY_API_KEY")
        return out

    def validate(self, *, require_keys: bool = False) -> Settings:
        """Validate every field type, bound, path, and environment guard. Never prints secret values."""
        errors: list[str] = []

        if self.env not in VALID_ENVS:
            errors.append(f"PRIORART_ENV={self.env!r}: must be one of {', '.join(VALID_ENVS)}")

        bounds = [
            ("PRIORART_MAX_CANDIDATES", self.max_candidates, 1, 12),
            ("PRIORART_RESULTS_PER_QUERY", self.results_per_query, 1, 10),
            ("PRIORART_PAGE_CHARS", self.page_chars, 1_000, 12_000),
            ("PRIORART_WORKERS", self.workers, 1, 16),
            ("PRIORART_SEARCH_CONCURRENCY", self.search_concurrency, 1, 16),
            ("PRIORART_MODEL_CONCURRENCY", self.model_concurrency, 1, 16),
            ("PRIORART_CHECK_DEADLINE_S", self.check_deadline_s, 10, 600),
            ("PRIORART_MAX_CONCURRENT_CHECKS", self.max_concurrent_checks, 1, 32),
            ("PRIORART_RATE_LIMIT_10M", self.rate_limit_10m, 1, 1000),
            ("PRIORART_RATE_LIMIT_DAY", self.rate_limit_day, 1, 10000),
            ("PRIORART_RATE_LIMIT_GLOBAL_HOUR", self.rate_limit_global_hour, 1, 10000),
            ("PRIORART_DAILY_BUDGET_CREDITS", self.daily_budget_credits, 1, 10_000_000),
            ("PRIORART_DAILY_BUDGET_OUTPUT_TOKENS", self.daily_budget_output_tokens, 1000, 1_000_000_000),
        ]
        for name, val, lo, hi in bounds:
            if isinstance(val, bool) or not isinstance(val, int) or not (lo <= val <= hi):
                errors.append(f"{name}={val!r}: must be an integer from {lo} to {hi}")

        if self.simulate and self.simulate not in SIMULATE_MODES:
            errors.append(f"PRIORART_SIMULATE={self.simulate!r}: must be one of {', '.join(SIMULATE_MODES)}")

        if not self.primary_model.strip():
            errors.append("PRIORART_PRIMARY_MODEL: must be non-empty")
        if not self.verifier_model.strip():
            errors.append("PRIORART_VERIFIER_MODEL: must be non-empty")

        parsed_base = urlsplit(self.nebius_base_url.strip())
        if parsed_base.scheme.lower() not in ("http", "https") or not parsed_base.netloc:
            errors.append(f"NEBIUS_BASE_URL={self.nebius_base_url!r}: must be a valid http:// or https:// URL")
        if self.public_origin:
            parsed_orig = urlsplit(self.public_origin.strip())
            if parsed_orig.scheme.lower() not in ("http", "https") or not parsed_orig.netloc:
                errors.append(f"PRIORART_PUBLIC_ORIGIN={self.public_origin!r}: must be a valid http:// or https:// URL")

        if _is_unsafe_dir(self.runs_dir):
            errors.append(f"PRIORART_RUNS_DIR={self.runs_dir!r}: cannot be filesystem root or home directory")
        if self.db_path and self.db_path != ":memory:" and _is_unsafe_dir(str(Path(self.db_path).parent)):
            errors.append(f"PRIORART_DB_PATH={self.db_path!r}: parent directory cannot be root or home directory")
        if self.cache_enabled and self.cache_path and _is_unsafe_dir(str(Path(self.cache_path).parent)):
            errors.append(f"PRIORART_CACHE_PATH={self.cache_path!r}: parent directory cannot be root or home directory")

        if self.is_production:
            if self.fake:
                errors.append("PRIORART_FAKE=1 is not allowed when PRIORART_ENV=production")
            if self.simulate:
                errors.append("PRIORART_SIMULATE is not allowed when PRIORART_ENV=production")
            missing = self.missing_keys()
            if missing:
                errors.append(f"Missing required production keys: {', '.join(missing)}")
        elif require_keys:
            missing = self.missing_keys()
            if missing:
                errors.append(f"Missing required keys: {', '.join(missing)}")

        if errors:
            raise ConfigError("\n".join(errors))
        return self

    @classmethod
    def from_env(cls, *, validate: bool = True, require_keys: bool = False) -> Settings:
        """Build Settings from the environment, collecting all parse and validation errors together."""
        parse_errors: list[str] = []

        def safe_int(name: str, default: int) -> int:
            try:
                return _int(name, default)
            except ConfigError as e:
                parse_errors.append(str(e))
                return default

        def safe_bool(name: str, default: bool) -> bool:
            try:
                return _bool(name, default)
            except ConfigError as e:
                parse_errors.append(str(e))
                return default

        s = cls(
            env=os.getenv("PRIORART_ENV", "development").strip().lower() or "development",
            nebius_api_key=os.getenv("NEBIUS_API_KEY", ""),
            nebius_base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            primary_model=os.getenv("PRIORART_PRIMARY_MODEL")
            or os.getenv("NEBIUS_MODEL")
            or "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            verifier_model=os.getenv("PRIORART_VERIFIER_MODEL", "Qwen/Qwen3-235B-A22B-Instruct-2507"),
            max_candidates=safe_int("PRIORART_MAX_CANDIDATES", 8),
            results_per_query=safe_int("PRIORART_RESULTS_PER_QUERY", 5),
            page_chars=safe_int("PRIORART_PAGE_CHARS", 9000),
            workers=safe_int("PRIORART_WORKERS", 6),
            search_concurrency=safe_int("PRIORART_SEARCH_CONCURRENCY", 14),
            model_concurrency=safe_int("PRIORART_MODEL_CONCURRENCY", 8),
            check_deadline_s=safe_int("PRIORART_CHECK_DEADLINE_S", 240),
            max_concurrent_checks=safe_int("PRIORART_MAX_CONCURRENT_CHECKS", 4),
            cache_enabled=safe_bool("PRIORART_CACHE", True),
            cache_path=os.getenv("PRIORART_CACHE_PATH", str(ROOT / ".cache" / "priorart.sqlite")),
            db_path=os.getenv("PRIORART_DB_PATH", str(ROOT / "data" / "priorart.sqlite")),
            runs_dir=os.getenv("PRIORART_RUNS_DIR", str(ROOT / "runs")),
            fake=safe_bool("PRIORART_FAKE", False),
            simulate=os.getenv("PRIORART_SIMULATE", "").strip(),
            allowed_hosts=_default_allowed_hosts(),
            public_origin=os.getenv("PRIORART_PUBLIC_ORIGIN", "").strip().rstrip("/"),
            trust_proxy_headers=safe_bool("PRIORART_TRUST_PROXY_HEADERS", False),
            rate_limit_10m=safe_int("PRIORART_RATE_LIMIT_10M", 3),
            rate_limit_day=safe_int("PRIORART_RATE_LIMIT_DAY", 15),
            rate_limit_global_hour=safe_int("PRIORART_RATE_LIMIT_GLOBAL_HOUR", 60),
            daily_budget_credits=safe_int("PRIORART_DAILY_BUDGET_CREDITS", 5000),
            daily_budget_output_tokens=safe_int("PRIORART_DAILY_BUDGET_OUTPUT_TOKENS", 2_000_000),
        )
        if validate:
            try:
                s.validate(require_keys=require_keys)
            except ConfigError as e:
                parse_errors.extend(str(e).splitlines())
        if parse_errors:
            raise ConfigError("\n".join(parse_errors))
        return s

    def masked_summary(self) -> dict[str, object]:
        """Safe diagnostic snapshot with secrets masked."""
        def _mask(k: str) -> str:
            if not k or k.startswith("your-"):
                return "<unset>"
            return f"***{k[-4:]}" if len(k) > 8 else "***"

        return {
            "version": VERSION,
            "env": self.env,
            "fake": self.fake,
            "simulate": self.simulate or None,
            "nebius_api_key": _mask(self.nebius_api_key),
            "tavily_api_key": _mask(self.tavily_api_key),
            "primary_model": self.primary_model,
            "verifier_model": self.verifier_model,
            "max_candidates": self.max_candidates,
            "results_per_query": self.results_per_query,
            "page_chars": self.page_chars,
            "workers": self.workers,
            "search_concurrency": self.search_concurrency,
            "model_concurrency": self.model_concurrency,
            "check_deadline_s": self.check_deadline_s,
            "max_concurrent_checks": self.max_concurrent_checks,
            "cache_enabled": self.cache_enabled,
            "allowed_hosts": self.allowed_hosts,
            "rate_limit_10m": self.rate_limit_10m,
            "rate_limit_day": self.rate_limit_day,
            "rate_limit_global_hour": self.rate_limit_global_hour,
            "daily_budget_credits": self.daily_budget_credits,
            "daily_budget_output_tokens": self.daily_budget_output_tokens,
        }

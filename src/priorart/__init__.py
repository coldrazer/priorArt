"""Prior Art: a research agent that tells you whether your idea already exists, and where the open space is."""
from __future__ import annotations

import threading
from typing import Any

from .cache import Cache
from .config import SIMULATE_MODES, VERSION, ConfigError, Settings
from .pipeline import Clients, Emit, PriorArt
from .resilience import CheckBudget, CircuitBreaker

__version__ = VERSION
__all__ = [
    "__version__",
    "VERSION",
    "Settings",
    "ConfigError",
    "PriorArt",
    "build",
    "Clients",
    "SIMULATE_MODES",
    "SharedRuntime",
    "close_shared_runtime",
]


class SharedRuntime:
    """Process-wide shared Cache, LLM, Search, and CircuitBreakers to prevent FD/TLS leaks."""

    def __init__(self, settings: Settings):
        self.key = self._key_for(settings)
        self.cache = Cache(settings.cache_path, settings.cache_enabled)
        self.llm_breaker = CircuitBreaker("nebius")
        self.search_breaker = CircuitBreaker("tavily")
        self.llm: Any = None
        self.search: Any = None
        if not settings.fake and not settings.missing_keys():
            from .llm import LLM
            from .search import Search

            self.llm = LLM(settings, self.cache, breaker=self.llm_breaker)
            self.search = Search(settings, self.cache, breaker=self.search_breaker)

    @staticmethod
    def _key_for(s: Settings) -> tuple[Any, ...]:
        return (
            s.fake,
            s.nebius_api_key,
            s.nebius_base_url,
            s.tavily_api_key,
            s.primary_model,
            s.verifier_model,
            s.cache_enabled,
            s.cache_path,
            s.results_per_query,
            s.page_chars,
        )

    def close(self) -> None:
        if self.llm is not None:
            self.llm.close()
        self.cache.close()


_RUNTIME_LOCK = threading.Lock()
_SHARED_RUNTIME: SharedRuntime | None = None


def get_shared_runtime(settings: Settings) -> SharedRuntime:
    """Return a cached process-wide runtime matching `settings`, recreating only if config changed."""
    global _SHARED_RUNTIME
    want = SharedRuntime._key_for(settings)
    with _RUNTIME_LOCK:
        if _SHARED_RUNTIME is not None and _SHARED_RUNTIME.key == want:
            return _SHARED_RUNTIME
        old = _SHARED_RUNTIME
        _SHARED_RUNTIME = SharedRuntime(settings)
    if old is not None:
        old.close()
    return _SHARED_RUNTIME


def close_shared_runtime() -> None:
    global _SHARED_RUNTIME
    with _RUNTIME_LOCK:
        old = _SHARED_RUNTIME
        _SHARED_RUNTIME = None
    if old is not None:
        old.close()


def build(
    settings: Settings | None = None,
    emit: Emit | None = None,
    *,
    runtime: SharedRuntime | None = None,
    budget: CheckBudget | None = None,
) -> PriorArt:
    """Wire up shared real clients (or fakes when PRIORART_FAKE=1)."""
    settings = settings or Settings()
    if settings.simulate and settings.is_production:
        raise ConfigError("PRIORART_SIMULATE is not allowed in production")
    if settings.fake:
        from .fakes import FakeLLM, FakeSearch

        clients = Clients(llm=FakeLLM(), search=FakeSearch())
    else:
        missing = settings.missing_keys()
        if missing:
            raise RuntimeError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in "
                "(or set PRIORART_FAKE=1 to try it offline)."
            )
        rt = runtime or get_shared_runtime(settings)
        if rt.llm is None or rt.search is None:
            from .llm import LLM
            from .search import Search

            rt.llm = LLM(settings, rt.cache, breaker=rt.llm_breaker)
            rt.search = Search(settings, rt.cache, breaker=rt.search_breaker)
        clients = Clients(llm=rt.llm, search=rt.search)
    if settings.simulate:
        clients.search = SimulatedOutage(clients.search, settings.simulate)
    return PriorArt(clients, settings, emit, budget=budget)


class SimulatedOutage:
    """Wraps the search client to fake a Tavily outage, so the failure case can be shown on demand.
    PRIORART_SIMULATE=search_down  -> every search raises
    PRIORART_SIMULATE=extract_down -> page extraction raises (agent falls back to search snippets)"""

    def __init__(self, inner: Any, mode: str):
        if mode not in SIMULATE_MODES:
            raise ValueError(f"PRIORART_SIMULATE must be one of {SIMULATE_MODES}")
        self.inner, self.mode = inner, mode

    def search(self, source: str, query: str, **kwargs: Any) -> Any:
        if self.mode == "search_down":
            raise ConnectionError("simulated Tavily outage")
        return self.inner.search(source, query, **kwargs) if kwargs else self.inner.search(source, query)

    def extract(self, urls: list[str], query: str | None = None, **kwargs: Any) -> Any:
        if self.mode == "extract_down":
            raise ConnectionError("simulated Tavily extract outage")
        return self.inner.extract(urls, query, **kwargs) if kwargs else self.inner.extract(urls, query)

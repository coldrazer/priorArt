"""Tavily search + extract, aimed at the places where prior art lives."""
from __future__ import annotations

import logging
import math
import time
from typing import Any
from urllib.parse import urlsplit

from .cache import Cache
from .config import Settings
from .log import get_logger, log_event
from .resilience import CheckBudget, CircuitBreaker, UsageRecord, retry_call

logger = get_logger("search")

# Each source type maps to a Tavily domain filter. "web" is unrestricted (company sites, blogs, docs).
SOURCES: dict[str, dict[str, Any]] = {
    "github": {"label": "GitHub", "include_domains": ["github.com"]},
    "hn": {"label": "Hacker News", "include_domains": ["news.ycombinator.com"]},
    "producthunt": {"label": "Product Hunt", "include_domains": ["producthunt.com"]},
    "arxiv": {"label": "arXiv", "include_domains": ["arxiv.org"]},
    "devpost": {"label": "Devpost", "include_domains": ["devpost.com"]},
    "web": {"label": "Web", "include_domains": None},
}


def safe_url(url: str | None) -> str:
    """Allow only http/https URLs with a non-empty host; reject javascript:, data:, file:, etc."""
    if not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return cleaned


def _extract_credits(resp: dict[str, Any], default: float) -> float:
    usage = resp.get("usage")
    if isinstance(usage, dict):
        credits = usage.get("credits")
        if isinstance(credits, (int, float)) and credits >= 0:
            return float(credits)
    return default


def _is_retryable_search_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__.lower()
    if any(w in name for w in ("timeout", "connection", "rate", "unavailable")):
        return True
    msg = str(exc).lower()
    return any(w in msg for w in ("429", "rate limit", "timed out", "temporarily unavailable", "502", "503", "504"))


def _is_auth_or_quota_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    if status in (401, 402, 403):
        return True
    msg = str(exc).lower()
    return any(w in msg for w in ("invalid api key", "unauthorized", "usage limit", "out of credits"))


class Search:
    def __init__(self, settings: Settings, cache: Cache, breaker: CircuitBreaker | None = None):
        from tavily import TavilyClient

        self.s = settings
        self.cache = cache
        self.breaker = breaker or CircuitBreaker("tavily")
        self.client: Any = TavilyClient(api_key=settings.tavily_api_key)

    def search(
        self,
        source: str,
        query: str,
        *,
        budget: CheckBudget | None = None,
        deadline: float | None = None,
        bypass_cache: bool = False,
    ) -> list[dict[str, Any]]:
        conf = SOURCES.get(source, SOURCES["web"])
        params: dict[str, Any] = dict(
            query=query[:390],  # Tavily recommends keeping queries under 400 chars
            search_depth="advanced",
            max_results=self.s.results_per_query,
            timeout=20,
            include_usage=True,
        )
        if conf["include_domains"]:
            params["include_domains"] = conf["include_domains"]

        cache_params = {k: v for k, v in params.items() if k not in ("timeout", "include_usage")}

        def compute() -> list[dict[str, Any]]:
            self.breaker.before_call()
            if budget is not None:
                budget.reserve_tavily(2.0)
            t0 = time.monotonic()
            attempt_num = 0

            def invoke() -> dict[str, Any]:
                nonlocal attempt_num
                attempt_num += 1
                return self.client.search(**params)

            try:
                r = retry_call(invoke, attempts=3, is_retryable=_is_retryable_search_error, deadline=deadline)
            except Exception as exc:
                self.breaker.record_failure(immediate=_is_auth_or_quota_error(exc))
                log_event(
                    logger,
                    logging.WARNING,
                    "upstream.failed",
                    provider="tavily",
                    operation="search",
                    source=source,
                    attempts=attempt_num,
                    error=type(exc).__name__,
                )
                raise

            self.breaker.record_success()
            credits = _extract_credits(r if isinstance(r, dict) else {}, 2.0)
            if budget is not None:
                budget.record(
                    UsageRecord(
                        provider="tavily",
                        operation="search",
                        credits=credits,
                        cached=False,
                        attempt=attempt_num,
                    )
                )
            log_event(
                logger,
                logging.INFO,
                "upstream.call",
                provider="tavily",
                operation="search",
                source=source,
                credits=credits,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                outcome="ok",
            )
            results: list[dict[str, Any]] = []
            for x in (r.get("results") or []) if isinstance(r, dict) else []:
                if not isinstance(x, dict):
                    continue
                url = safe_url(x.get("url"))
                if not url:
                    continue
                try:
                    score = float(x.get("score") or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                results.append(
                    {
                        "title": str(x.get("title") or "")[:300],
                        "url": url,
                        "snippet": str(x.get("content") or "")[:600],
                        "score": max(0.0, score),
                    }
                )
            return results

        value, was_cached = self.cache.get_or_compute("tavily_search", cache_params, compute, bypass=bypass_cache)
        if was_cached and budget is not None:
            budget.record(UsageRecord(provider="tavily", operation="search", cached=True))
        return value

    def extract(
        self,
        urls: list[str],
        query: str | None = None,
        *,
        budget: CheckBudget | None = None,
        deadline: float | None = None,
        bypass_cache: bool = False,
    ) -> dict[str, str]:
        """Return {url: page_text}. Cached per individual URL so overlapping runs hit cache."""
        clean_urls: list[str] = []
        seen: set[str] = set()
        for raw in urls:
            u = safe_url(raw)
            if u and u not in seen:
                seen.add(u)
                clean_urls.append(u)

        out: dict[str, str] = {}
        missing: list[str] = []
        max_store_chars = max(2 * self.s.page_chars, 18_000)

        for u in clean_urls:
            if not bypass_cache:
                hit, cached_text = self.cache.get("tavily_extract_url", {"url": u, "depth": "advanced"})
                if hit and isinstance(cached_text, str):
                    out[u] = cached_text
                    if budget is not None:
                        budget.record(UsageRecord(provider="tavily", operation="extract", cached=True))
                    continue
            missing.append(u)

        for i in range(0, len(missing), 20):  # Tavily extract accepts up to 20 URLs per call
            batch = missing[i : i + 20]
            params: dict[str, Any] = dict(
                urls=batch,
                extract_depth="advanced",
                format="text",
                timeout=30,
                include_usage=True,
            )
            est_credits = float(math.ceil(len(batch) / 5) * 2)
            self.breaker.before_call()
            if budget is not None:
                budget.reserve_tavily(est_credits)
            t0 = time.monotonic()
            attempt_num = 0

            def invoke(p: dict[str, Any] = params) -> dict[str, Any]:
                nonlocal attempt_num
                attempt_num += 1
                return self.client.extract(**p)

            try:
                r = retry_call(invoke, attempts=3, is_retryable=_is_retryable_search_error, deadline=deadline)
            except Exception as exc:
                self.breaker.record_failure(immediate=_is_auth_or_quota_error(exc))
                log_event(
                    logger,
                    logging.WARNING,
                    "upstream.failed",
                    provider="tavily",
                    operation="extract",
                    batch_size=len(batch),
                    attempts=attempt_num,
                    error=type(exc).__name__,
                )
                raise

            self.breaker.record_success()
            credits = _extract_credits(r if isinstance(r, dict) else {}, est_credits)
            if budget is not None:
                budget.record(
                    UsageRecord(
                        provider="tavily",
                        operation="extract",
                        credits=credits,
                        cached=False,
                        attempt=attempt_num,
                    )
                )
            log_event(
                logger,
                logging.INFO,
                "upstream.call",
                provider="tavily",
                operation="extract",
                batch_size=len(batch),
                credits=credits,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                outcome="ok",
            )
            for x in (r.get("results") or []) if isinstance(r, dict) else []:
                if not isinstance(x, dict):
                    continue
                u = safe_url(x.get("url"))
                if not u:
                    continue
                text = str(x.get("raw_content") or "")[:max_store_chars]
                out[u] = text
                if text:
                    self.cache.put("tavily_extract_url", {"url": u, "depth": "advanced"}, text)
        return out

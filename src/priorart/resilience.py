"""Retries with jitter, circuit breakers, and per-check cost budgets."""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from .log import get_logger, log_event

T = TypeVar("T")
logger = get_logger("resilience")


class ProviderUnavailable(RuntimeError):
    """Raised when a provider circuit breaker is open."""

    def __init__(self, provider: str, message: str = ""):
        self.provider = provider
        super().__init__(message or f"{provider} service is temporarily unavailable")


class BudgetExceeded(RuntimeError):
    """Raised when a check or daily spend budget is exhausted."""


def _retry_after_seconds(exc: BaseException) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        val = float(raw)
        return max(0.0, min(val, 30.0))
    except (TypeError, ValueError):
        return None


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    is_retryable: Callable[[BaseException], bool],
    deadline: float | None = None,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Execute `fn` up to `attempts` times with full-jitter backoff. Never sleeps after the final attempt."""
    last_err: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except BaseException as exc:
            last_err = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            retry_after = _retry_after_seconds(exc)
            cap = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = retry_after if retry_after is not None else random.uniform(0.0, cap)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            time.sleep(delay)
    assert last_err is not None
    raise last_err


class CircuitBreaker:
    """Thread-safe per-provider circuit breaker (closed -> open -> half_open)."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        window_s: float = 30.0,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: deque[float] = deque()
        self._state = "closed"
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    def _maybe_half_open_locked(self) -> None:
        if self._state == "open" and self._opened_at is not None:
            if (self._clock() - self._opened_at) >= self.cooldown_s:
                self._state = "half_open"
                self._trial_in_flight = False

    def before_call(self) -> None:
        with self._lock:
            self._maybe_half_open_locked()
            if self._state == "open":
                raise ProviderUnavailable(self.name)
            if self._state == "half_open":
                if self._trial_in_flight:
                    raise ProviderUnavailable(self.name)
                self._trial_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._state = "closed"
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self, *, immediate: bool = False) -> None:
        now = self._clock()
        with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = now
                self._trial_in_flight = False
                log_event(logger, logging.WARNING, "breaker.reopened", provider=self.name)
                return
            if immediate:
                self._state = "open"
                self._opened_at = now
                self._trial_in_flight = False
                log_event(logger, logging.WARNING, "breaker.opened", provider=self.name, reason="immediate")
                return
            cutoff = now - self.window_s
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            self._failures.append(now)
            if len(self._failures) >= self.failure_threshold:
                self._state = "open"
                self._opened_at = now
                self._trial_in_flight = False
                log_event(
                    logger,
                    logging.WARNING,
                    "breaker.opened",
                    provider=self.name,
                    failures=len(self._failures),
                )

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._state = "closed"
            self._opened_at = None
            self._trial_in_flight = False


@dataclass
class UsageRecord:
    provider: str
    operation: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    credits: float = 0.0
    cached: bool = False
    attempt: int = 1


@dataclass
class CheckBudget:
    """Thread-safe per-check resource ceiling and usage accumulator."""

    max_model_calls: int = 24
    max_output_tokens: int = 40_000
    max_input_chars: int = 180_000
    max_tavily_credits: float = 40.0
    on_usage: Callable[[UsageRecord], None] | None = None

    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    input_chars: int = 0
    tavily_credits: float = 0.0
    cache_hits: int = 0
    records: list[UsageRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def reserve_model_call(self, *, input_chars: int, max_tokens: int) -> None:
        with self._lock:
            if self.model_calls + 1 > self.max_model_calls:
                raise BudgetExceeded("Check exceeded maximum model call budget")
            if self.input_chars + input_chars > self.max_input_chars:
                raise BudgetExceeded("Check exceeded maximum model input budget")
            if self.completion_tokens + max_tokens > self.max_output_tokens + max_tokens:
                raise BudgetExceeded("Check exceeded maximum output token budget")
            self.model_calls += 1
            self.input_chars += input_chars

    def reserve_tavily(self, credits: float) -> None:
        with self._lock:
            if self.tavily_credits + credits > self.max_tavily_credits:
                raise BudgetExceeded("Check exceeded maximum search credit budget")
            self.tavily_credits += credits

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self.records.append(rec)
            if rec.cached:
                self.cache_hits += 1
            else:
                self.prompt_tokens += max(0, rec.prompt_tokens)
                self.completion_tokens += max(0, rec.completion_tokens)
        if self.on_usage is not None:
            try:
                self.on_usage(rec)
            except Exception as exc:
                log_event(logger, logging.WARNING, "usage.record_failed", error=type(exc).__name__)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_calls": self.model_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "tavily_credits": round(self.tavily_credits, 2),
                "cache_hits": self.cache_hits,
            }

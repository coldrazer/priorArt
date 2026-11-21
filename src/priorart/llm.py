"""Nebius Token Factory client (OpenAI-compatible) with strict JSON parsing, retries, and usage tracking."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from .cache import Cache
from .config import Settings
from .log import get_logger, log_event
from .resilience import CheckBudget, CircuitBreaker, UsageRecord, retry_call

logger = get_logger("llm")

_THINK_CLOSED = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^\s*<think>", re.IGNORECASE)
_OPEN_FENCE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_CLOSE_FENCE = re.compile(r"\s*```$")

MAX_PAYLOAD_CHARS = 140_000
_LIST_WRAPPER_KEYS: dict[str, str] = {
    "decompose": "features",
    "triage": "picked",
    "verify": "results",
    "pivots": "pivots",
}
_TASK_SCHEMA_HINTS: dict[str, str] = {
    "decompose": '{"summary": "...", "target_user": "...", "features": [{"id": "F1", "name": "...", "description": "..."}], "queries": [{"source": "github", "query": "..."}]}',
    "triage": '{"picked": [{"id": "c1", "name": "...", "why": "..."}]}',
    "compare": '{"project_name": "...", "one_liner": "...", "kind": "repo|product|paper|hackathon|other", "features": [{"id": "F1", "score": 0, "quote": ""}]}',
    "verify": '{"results": [{"key": "F1", "supported": true, "adjusted_score": 2, "reason": "..."}]}',
    "pivots": '{"verdict": "...", "pivots": [{"title": "...", "pitch": "...", "why_open": "..."}]}',
}


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (handles <think> blocks, fences, and prose)."""
    cleaned = _THINK_CLOSED.sub("", text or "").strip()
    if _THINK_OPEN.match(cleaned):
        cleaned = _THINK_OPEN.sub("", cleaned).strip()
    if cleaned.startswith("```"):
        cleaned = _OPEN_FENCE.sub("", cleaned, count=1).strip()
        cleaned = _CLOSE_FENCE.sub("", cleaned, count=1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    # Prefer JSON objects before arrays when prose wraps the reply.
    for target in ("{", "["):
        pos = 0
        while True:
            idx = cleaned.find(target, pos)
            if idx == -1:
                break
            try:
                obj, _ = decoder.raw_decode(cleaned, idx)
                return obj
            except json.JSONDecodeError:
                pos = idx + 1
    raise LLMError(f"Model did not return valid JSON (len={len(cleaned)}).")


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    resp_code = getattr(resp, "status_code", None)
    return resp_code if isinstance(resp_code, int) else None


def _is_quota_error(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code == 402:
        return True
    msg = str(exc).lower()
    return any(w in msg for w in ("insufficient_quota", "billing", "out of credits", "quota exceeded"))


class LLM:
    def __init__(self, settings: Settings, cache: Cache, breaker: CircuitBreaker | None = None):
        from openai import OpenAI

        self.s = settings
        self.cache = cache
        self.breaker = breaker or CircuitBreaker("nebius")
        self.client: Any = OpenAI(
            base_url=settings.nebius_base_url,
            api_key=settings.nebius_api_key,
            timeout=60.0,
            max_retries=0,
        )
        self._json_mode_ok: dict[str, bool] = {}
        self._json_mode_lock = threading.Lock()

    def close(self) -> None:
        if hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                pass

    def model_for(self, role: str) -> str:
        return self.s.verifier_model if role == "verifier" else self.s.primary_model

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self.client.models.list())

    def _create(self, model: str, kwargs: dict[str, Any]) -> tuple[str, int, int]:
        """One completion. Uses JSON mode when supported and remembers per model under a lock."""
        import openai

        with self._json_mode_lock:
            use_json = self._json_mode_ok.get(model, True)

        if use_json:
            try:
                r = self.client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
                return self._extract_reply(r)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                msg = str(e).lower()
                if "response_format" not in msg and "json" not in msg:
                    raise
                with self._json_mode_lock:
                    self._json_mode_ok[model] = False
        r = self.client.chat.completions.create(**kwargs)
        return self._extract_reply(r)

    @staticmethod
    def _extract_reply(r: Any) -> tuple[str, int, int]:
        choices = getattr(r, "choices", None) or []
        if not choices or getattr(choices[0], "message", None) is None:
            raise LLMError("Model returned an empty completion response.")
        content = choices[0].message.content or ""
        usage = getattr(r, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return content, prompt_tokens, completion_tokens

    def _call(
        self,
        task: str,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        *,
        budget: CheckBudget | None = None,
        deadline: float | None = None,
    ) -> str:
        import openai

        self.breaker.before_call()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs: dict[str, Any] = dict(model=model, messages=messages, temperature=0.1, max_tokens=max_tokens)
        wrong_model = (
            f"Model '{model}' isn't available on your Token Factory key. Run `python src/agent.py models` "
            "and set PRIORART_PRIMARY_MODEL / PRIORART_VERIFIER_MODEL in .env."
        )

        retryable_types = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
            ConnectionError,
            TimeoutError,
        )

        attempt_num = 0
        t0 = time.monotonic()

        def is_retryable(exc: BaseException) -> bool:
            if _is_quota_error(exc):
                return False
            if isinstance(exc, retryable_types):
                return True
            code = _status_code(exc)
            return code is not None and (code == 429 or code >= 500)

        def invoke() -> str:
            nonlocal attempt_num
            attempt_num += 1
            if budget is not None:
                budget.reserve_model_call(input_chars=len(system) + len(user), max_tokens=max_tokens)
            try:
                content, p_tok, c_tok = self._create(model, kwargs)
            except openai.AuthenticationError as e:
                self.breaker.record_failure(immediate=True)
                raise LLMError("Token Factory rejected the API key (401). Check NEBIUS_API_KEY in .env.") from e
            except openai.NotFoundError as e:
                raise LLMError(wrong_model) from e
            except (openai.BadRequestError, openai.PermissionDeniedError, openai.UnprocessableEntityError) as e:
                if _is_quota_error(e):
                    self.breaker.record_failure(immediate=True)
                msg = str(e).lower()
                if "model" in msg and any(
                    w in msg for w in ("not found", "does not exist", "unknown", "not available", "invalid model")
                ):
                    raise LLMError(wrong_model) from e
                raise LLMError(f"Token Factory refused the request for {model}: {type(e).__name__}") from e
            except Exception as e:
                if _is_quota_error(e):
                    self.breaker.record_failure(immediate=True)
                    raise LLMError("Token Factory quota or billing limit reached.") from e
                raise

            self.breaker.record_success()
            if budget is not None:
                budget.record(
                    UsageRecord(
                        provider="nebius",
                        operation=task,
                        model=model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        cached=False,
                        attempt=attempt_num,
                    )
                )
            log_event(
                logger,
                logging.INFO,
                "upstream.call",
                provider="nebius",
                operation=task,
                model=model,
                attempt=attempt_num,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
                outcome="ok",
            )
            return content

        try:
            return retry_call(
                invoke,
                attempts=3,
                is_retryable=is_retryable,
                deadline=deadline,
                on_retry=lambda att, err, delay: log_event(
                    logger,
                    logging.WARNING,
                    "upstream.retry",
                    provider="nebius",
                    operation=task,
                    model=model,
                    attempt=att,
                    delay_s=round(delay, 2),
                    error=type(err).__name__,
                ),
            )
        except LLMError:
            raise
        except Exception as exc:
            self.breaker.record_failure()
            log_event(
                logger,
                logging.WARNING,
                "upstream.failed",
                provider="nebius",
                operation=task,
                model=model,
                attempts=attempt_num,
                error=type(exc).__name__,
            )
            raise LLMError(f"Token Factory call failed for {model} after {attempt_num} tries: {type(exc).__name__}") from exc

    def _coerce_shape(self, task: str, parsed: Any, expect: str) -> Any:
        if expect != "object":
            return parsed
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and task in _LIST_WRAPPER_KEYS:
            return {_LIST_WRAPPER_KEYS[task]: parsed}
        raise LLMError(f"Expected JSON object for task '{task}', got {type(parsed).__name__}.")

    def json(
        self,
        task: str,
        system: str,
        payload: dict[str, Any],
        role: str = "primary",
        max_tokens: int = 3000,
        *,
        expect: str = "object",
        budget: CheckBudget | None = None,
        deadline: float | None = None,
        bypass_cache: bool = False,
    ) -> Any:
        """Send `payload` as JSON, get JSON back. Cached on (model, task, prompt, payload, max_tokens)."""
        model = self.model_for(role)
        user = json.dumps(payload, ensure_ascii=False)
        if len(user) > MAX_PAYLOAD_CHARS:
            raise LLMError(f"Payload for '{task}' exceeds size limit ({len(user)} > {MAX_PAYLOAD_CHARS} chars).")

        def compute() -> Any:
            text = self._call(task, model, system, user, max_tokens, budget=budget, deadline=deadline)
            try:
                return self._coerce_shape(task, parse_json(text), expect)
            except LLMError:
                hint = _TASK_SCHEMA_HINTS.get(task, '{"...": "..."}')
                repair_tokens = min(max_tokens, max(256, len(text) // 2 + 256))
                fix = self._call(
                    f"{task}_repair",
                    model,
                    f"Return ONLY a valid JSON object matching this shape: {hint}. No prose, no code fences.",
                    f"Convert this into a single valid JSON object:\n{text[:6000]}",
                    repair_tokens,
                    budget=budget,
                    deadline=deadline,
                )
                return self._coerce_shape(task, parse_json(fix), expect)

        cache_key = {"m": model, "t": task, "s": system, "u": user, "mt": max_tokens}
        value, was_cached = self.cache.get_or_compute("llm", cache_key, compute, bypass=bypass_cache)
        if was_cached and budget is not None:
            budget.record(UsageRecord(provider="nebius", operation=task, model=model, cached=True))
        return value

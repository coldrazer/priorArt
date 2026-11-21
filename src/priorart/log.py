"""Structured JSON logging with context propagation and automatic redaction."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from .config import VERSION

T = TypeVar("T")

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
check_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("check_id", default=None)
owner_ref_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("owner_ref", default=None)

SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "token", "cookie", "set-cookie", "password",
    "secret", "idea", "query", "prompt", "raw_content", "page", "nebius_api_key", "tavily_api_key",
})

_SECRET_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\btvly-[A-Za-z0-9_\-]{6,}"), "[REDACTED_TAVILY_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]


def scrub_text(text: str) -> str:
    out = text
    for rx, repl in _SECRET_PATTERNS:
        out = rx.sub(repl, out)
    return out


def redact(value: Any, *, _key: str = "") -> Any:
    """Recursively redact sensitive keys and secret-like strings from log payloads."""
    if _key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, env: str | None = None):
        super().__init__()
        self.env = env or os.getenv("PRIORART_ENV", "development")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "event": getattr(record, "event", record.name),
            "message": scrub_text(record.getMessage()),
            "service": "prior-art",
            "env": self.env,
            "version": VERSION,
        }
        rid = request_id_var.get()
        cid = check_id_var.get()
        oref = owner_ref_var.get()
        if rid:
            payload["request_id"] = rid
        if cid:
            payload["check_id"] = cid
        if oref:
            payload["owner_ref"] = oref
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for k, v in redact(extra).items():
                if k not in payload:
                    payload[k] = v
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["traceback"] = scrub_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(env: str | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("priorart")
    logger.setLevel(level)
    if not any(isinstance(getattr(h, "formatter", None), JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter(env=env))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "priorart") -> logging.Logger:
    if not name.startswith("priorart"):
        name = f"priorart.{name}"
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, message: str = "", **fields: Any) -> None:
    logger.log(level, message or event, extra={"event": event, "fields": fields})


def capture_error(
    exc: BaseException,
    *,
    layer: str,
    check_id: str | None = None,
    request_id: str | None = None,
    **fields: Any,
) -> str:
    """Log an exception with traceback server-side and return an opaque client-safe reference id."""
    ref = f"err_{secrets.token_hex(4)}"
    logger = get_logger(layer)
    extra_fields = {
        "error_ref": ref,
        "layer": layer,
        "exc_type": type(exc).__name__,
        **fields,
    }
    if check_id:
        extra_fields["check_id"] = check_id
    if request_id:
        extra_fields["request_id"] = request_id
    logger.error(
        f"{layer} failure ({ref}): {type(exc).__name__}",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"event": "error.captured", "fields": extra_fields},
    )
    return ref


def propagate_context(fn: Callable[..., T]) -> Callable[..., T]:
    """Capture the current contextvars so background threads keep request_id/check_id/owner_ref."""
    ctx = contextvars.copy_context()

    def wrapped(*args: Any, **kwargs: Any) -> T:
        return ctx.run(fn, *args, **kwargs)

    return wrapped

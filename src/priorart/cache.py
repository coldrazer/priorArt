"""Resilient SQLite cache for API calls with WAL, TTLs, single-flight, and LRU eviction.

Caching is strictly best-effort: a locked or unwritable cache degrades to a cache miss with a warning
and never fails a check.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .log import get_logger, log_event

logger = get_logger("cache")

DEFAULT_TTLS: dict[str, float] = {
    "tavily_search": 24 * 3600.0,
    "tavily_extract": 24 * 3600.0,
    "llm": 7 * 24 * 3600.0,
}
DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MB


class _InFlight:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


class Cache:
    def __init__(
        self,
        path: str,
        enabled: bool = True,
        *,
        ttls: dict[str, float] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.path = path
        self.enabled = enabled
        self.ttls = {**DEFAULT_TTLS, **(ttls or {})}
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._flight_lock = threading.Lock()
        self._inflight: dict[str, _InFlight] = {}
        self._conn: sqlite3.Connection | None = None
        if enabled:
            self._open()

    def _open(self) -> None:
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "k TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL DEFAULT '', "
                "v TEXT NOT NULL, "
                "created_at REAL NOT NULL DEFAULT 0, "
                "last_used_at REAL NOT NULL DEFAULT 0, "
                "bytes INTEGER NOT NULL DEFAULT 0)"
            )
            # Migrate legacy 2-column kv(k, v) tables cleanly if an old cache file exists.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(kv)")}
            for col, ddl in (
                ("kind", "TEXT NOT NULL DEFAULT ''"),
                ("created_at", "REAL NOT NULL DEFAULT 0"),
                ("last_used_at", "REAL NOT NULL DEFAULT 0"),
                ("bytes", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in cols:
                    conn.execute(f"ALTER TABLE kv ADD COLUMN {col} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS kv_kind_created ON kv (kind, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS kv_last_used ON kv (last_used_at)")
            conn.commit()
            self._conn = conn
        except (OSError, sqlite3.Error) as exc:
            self.enabled = False
            self._conn = None
            log_event(logger, logging.WARNING, "cache.disabled", error=type(exc).__name__)

    @staticmethod
    def key(kind: str, payload: Any) -> str:
        raw = kind + "\n" + json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _ttl_for(self, kind: str) -> float:
        return self.ttls.get(kind, 24 * 3600.0)

    def _is_empty(self, value: Any) -> bool:
        return value is None or value == [] or value == {} or value == ""

    def get(self, kind: str, payload: Any) -> tuple[bool, Any]:
        """Return (hit, value). Expired or unreadable entries count as a miss."""
        if not self.enabled or self._conn is None:
            return False, None
        k = self.key(kind, payload)
        now = time.time()
        ttl = self._ttl_for(kind)
        try:
            with self._lock:
                if self._conn is None:
                    return False, None
                row = self._conn.execute("SELECT v, created_at FROM kv WHERE k = ?", (k,)).fetchone()
                if not row:
                    return False, None
                raw_v, created_at = row
                if created_at and (now - created_at) > ttl:
                    self._conn.execute("DELETE FROM kv WHERE k = ?", (k,))
                    self._conn.commit()
                    return False, None
                self._conn.execute("UPDATE kv SET last_used_at = ? WHERE k = ?", (now, k))
                self._conn.commit()
            return True, json.loads(raw_v)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            log_event(logger, logging.WARNING, "cache.read_error", kind=kind, error=type(exc).__name__)
            return False, None

    def put(self, kind: str, payload: Any, value: Any) -> None:
        """Persist a non-empty value; best-effort (never raises on SQLite errors)."""
        if not self.enabled or self._conn is None or self._is_empty(value):
            return
        k = self.key(kind, payload)
        now = time.time()
        try:
            encoded = json.dumps(value, ensure_ascii=False)
            size = len(encoded.encode("utf-8"))
            with self._lock:
                if self._conn is None:
                    return
                self._conn.execute(
                    "INSERT OR REPLACE INTO kv (k, kind, v, created_at, last_used_at, bytes) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (k, kind, encoded, now, now, size),
                )
                self._evict_locked()
                self._conn.commit()
        except sqlite3.Error as exc:
            log_event(logger, logging.WARNING, "cache.write_error", kind=kind, error=type(exc).__name__)

    def _evict_locked(self) -> None:
        if self._conn is None or self.max_bytes <= 0:
            return
        total = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM kv").fetchone()[0]
        if total <= self.max_bytes:
            return
        target = int(self.max_bytes * 0.85)
        rows = self._conn.execute("SELECT k, bytes FROM kv ORDER BY last_used_at ASC LIMIT 200").fetchall()
        to_drop: list[str] = []
        for k, b in rows:
            to_drop.append(k)
            total -= b
            if total <= target:
                break
        if to_drop:
            marks = ",".join("?" for _ in to_drop)
            self._conn.execute(f"DELETE FROM kv WHERE k IN ({marks})", to_drop)

    def get_or_compute(
        self,
        kind: str,
        payload: Any,
        fn: Callable[[], Any],
        *,
        bypass: bool = False,
        wait_timeout: float = 30.0,
    ) -> tuple[Any, bool]:
        """Returns (value, was_cached). Deduplicates concurrent calls for the same key (single-flight)."""
        if bypass:
            value = fn()
            self.put(kind, payload, value)
            return value, False

        if self.enabled:
            hit, cached = self.get(kind, payload)
            if hit:
                return cached, True

        k = self.key(kind, payload)
        with self._flight_lock:
            existing = self._inflight.get(k)
            if existing is None:
                flight = _InFlight()
                self._inflight[k] = flight
                is_leader = True
            else:
                flight = existing
                is_leader = False

        if not is_leader:
            if flight.event.wait(timeout=wait_timeout):
                if flight.error is None:
                    return flight.value, True
                # Leader failed; fall through to try computing independently.
            return fn(), False

        try:
            value = fn()
            flight.value = value
            self.put(kind, payload, value)
            return value, False
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            with self._flight_lock:
                self._inflight.pop(k, None)
            flight.event.set()

    def healthy(self) -> bool:
        if not self.enabled:
            return False
        try:
            with self._lock:
                if self._conn is None:
                    return False
                self._conn.execute("SELECT 1").fetchone()
                return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

"""SQLite connections for Prior Art's database (see docs/DATA_MODEL.md, docs/MIGRATIONS.md)."""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import ROOT
from .log import get_logger, log_event

logger = get_logger("db")

MIN_SQLITE = (3, 37, 0)  # STRICT tables
DEFAULT_DB = ROOT / "data" / "priorart.sqlite"
MIGRATIONS_DIR = ROOT / "migrations"
MODEL_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


class DatabaseError(RuntimeError):
    pass


def db_path() -> Path:
    return Path(os.getenv("PRIORART_DB_PATH") or DEFAULT_DB)


def connect(path: str | Path | None = None, *, shared: bool = False) -> sqlite3.Connection:
    """Open the database with the settings every connection needs.

    - autocommit mode (isolation_level=None): transactions are explicit, so migrations control their own
    - foreign keys ON: SQLite does not enforce them otherwise
    - busy_timeout BEFORE WAL journal mode, plus NORMAL sync and temp_store in memory
    - shared=True lets the connection move between threads (the Pool hands each to one thread at a time)
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise DatabaseError(
            f"SQLite {sqlite3.sqlite_version} is too old: Prior Art needs "
            f"{'.'.join(map(str, MIN_SQLITE))}+ for STRICT tables. Upgrade Python or its SQLite."
        )
    target = str(path if path is not None else db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=not shared)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    if target != ":memory:":
        # busy_timeout MUST precede journal_mode=WAL so concurrent opens wait instead of raising locked.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA journal_size_limit = 67108864")
    return conn


class PoolTimeout(DatabaseError):
    pass


class Pool:
    """Connections for the long-lived server process (docs/DATA_LIFECYCLE.md §6).

    SQLite allows one writer at a time per database file, so there is exactly ONE write connection, handed out
    under a lock: more would only queue on SQLite's own file lock. Reads run in parallel under WAL, from a small
    set of read-only connections (`PRAGMA query_only`), so a read can never write by mistake.
    """

    def __init__(self, path: str | Path | None = None, readers: int | None = None, timeout: float = 5.0):
        target = str(path if path is not None else db_path())
        if target == ":memory:":
            raise DatabaseError("a Pool needs a database file: each :memory: connection is a separate database")
        self.path = target
        self.size = readers if readers is not None else int(os.getenv("PRIORART_DB_READERS", "4"))
        if self.size < 1:
            raise DatabaseError("a Pool needs at least one read connection")
        self.timeout = timeout
        self._closed = False
        self._writer = connect(target, shared=True)
        self._write_lock = threading.Lock()
        self._readers: queue.Queue[sqlite3.Connection] = queue.Queue()
        self._all_readers: list[sqlite3.Connection] = []
        try:
            for _ in range(self.size):
                conn = connect(target, shared=True)
                conn.execute("PRAGMA query_only = ON")
                self._all_readers.append(conn)
                self._readers.put(conn)
        except Exception:
            self._writer.close()
            for c in self._all_readers:
                c.close()
            raise

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise DatabaseError("connection pool is closed")
        if not self._write_lock.acquire(timeout=self.timeout):
            raise PoolTimeout(f"no write connection free after {self.timeout}s")
        try:
            yield self._writer
        finally:
            if self._writer.in_transaction:
                log_event(logger, logging.WARNING, "db.stale_write_txn_rolled_back")
                try:
                    self._writer.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            self._write_lock.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise DatabaseError("connection pool is closed")
        try:
            conn = self._readers.get(timeout=self.timeout)
        except queue.Empty:
            raise PoolTimeout(f"all {self.size} read connections busy for {self.timeout}s") from None
        try:
            yield conn
        finally:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if self._closed:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            else:
                self._readers.put(conn)

    def close(self) -> None:
        self._closed = True
        with self._write_lock:
            try:
                if self._writer.in_transaction:
                    self._writer.execute("ROLLBACK")
                self._writer.execute("PRAGMA optimize")
            except sqlite3.Error:
                pass
            try:
                self._writer.close()
            except sqlite3.Error:
                pass
        while True:
            try:
                conn = self._readers.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except sqlite3.Error:
                pass

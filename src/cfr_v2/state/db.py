"""P2 database access on a plain DB-API driver: no ORM, no SQLAlchemy (LDR-0011).

Works with ``psycopg`` (3) or ``psycopg2``; both use ``%(name)s`` parameters and
decode ``jsonb`` to Python values. JSON is always sent as text and cast in SQL
(``%(x)s::jsonb``), so no driver-specific adapters are needed.

``Database`` is a small thread-safe connection pool. ``transaction()`` yields a
``Tx`` that commits on success and rolls back on error; reads use it too.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from cfr_v2.telemetry import get_logger

logger = get_logger(__name__)

Params = Mapping[str, Any] | None


@dataclass(frozen=True)
class Driver:
    name: str
    connect: Callable[[str], Any]
    Error: type[Exception]  # DB-API base error of the driver
    is_broken: Callable[[Any], bool]


def _psycopg3() -> Driver:
    import psycopg  # raises ImportError when libpq is missing and no binary package is installed

    return Driver(
        name="psycopg",
        connect=lambda dsn: psycopg.connect(dsn, autocommit=False),
        Error=psycopg.Error,
        is_broken=lambda conn: conn.closed or conn.broken,
    )


def _psycopg2() -> Driver:
    import psycopg2

    return Driver(
        name="psycopg2",
        connect=lambda dsn: psycopg2.connect(dsn),
        Error=psycopg2.Error,
        is_broken=lambda conn: conn.closed != 0,
    )


_LOADERS = {"psycopg": _psycopg3, "psycopg2": _psycopg2}


def load_driver(name: str | None = None) -> Driver:
    """``psycopg`` (3), ``psycopg2``, or ``auto`` (default: psycopg 3, else psycopg2)."""
    name = name or os.environ.get("CFR_DB_DRIVER", "auto")
    if name != "auto":
        return _LOADERS[name]()
    try:
        return _psycopg3()
    except ImportError:
        return _psycopg2()


_SQLALCHEMY_SCHEME = re.compile(r"^postgres(?:ql)?\+[\w]+://")


def libpq_dsn(url: str) -> str:
    """Accept ``postgresql+psycopg://`` style URLs (earlier configs) as libpq URLs."""
    return _SQLALCHEMY_SCHEME.sub("postgresql://", url)


class Tx:
    """One open transaction on one connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def execute(self, sql: str, params: Params = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def one(self, sql: str, params: Params = None) -> tuple | None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def scalar(self, sql: str, params: Params = None) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def all(self, sql: str, params: Params = None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def column(self, sql: str, params: Params = None) -> list[Any]:
        return [row[0] for row in self.all(sql, params)]

    def rows(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


class PoolTimeoutError(RuntimeError):
    pass


class Database:
    """Thread-safe pool of up to ``max_connections`` connections, opened lazily."""

    def __init__(
        self,
        url: str,
        *,
        max_connections: int = 10,
        recycle_sec: float = 1800,
        acquire_timeout_sec: float = 30,
        driver: Driver | None = None,
    ) -> None:
        self.dsn = libpq_dsn(url)
        self.driver = driver or load_driver()
        self.max_connections = max_connections
        self.recycle_sec = recycle_sec
        self.acquire_timeout_sec = acquire_timeout_sec
        self._idle: queue.LifoQueue[tuple[Any, float]] = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._closed = False

    def _acquire(self) -> tuple[Any, float]:
        if not self._slots.acquire(timeout=self.acquire_timeout_sec):
            raise PoolTimeoutError(f"no database connection free within {self.acquire_timeout_sec}s")
        try:
            while True:
                try:
                    conn, opened = self._idle.get_nowait()
                except queue.Empty:
                    return self.driver.connect(self.dsn), time.monotonic()
                if self.driver.is_broken(conn) or time.monotonic() - opened > self.recycle_sec:
                    _close_quietly(conn)
                    continue
                return conn, opened
        except BaseException:
            self._slots.release()
            raise

    def _release(self, conn: Any, opened: float, *, discard: bool) -> None:
        try:
            if discard or self._closed or self.driver.is_broken(conn):
                _close_quietly(conn)
            else:
                self._idle.put((conn, opened))
        finally:
            self._slots.release()

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        conn, opened = self._acquire()
        discard = False
        try:
            yield Tx(conn)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:  # connection already unusable
                discard = True
            raise
        finally:
            self._release(conn, opened, discard=discard)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            _close_quietly(conn)


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # already closed or broken
        logger.debug("db: error closing connection", exc_info=True)


@contextmanager
def autocommit_connection(url: str, driver: Driver | None = None) -> Iterator[Tx]:
    """A standalone autocommit connection, for statements that cannot run in a
    transaction (``CREATE DATABASE``) and for admin tooling."""
    driver = driver or load_driver()
    conn = driver.connect(libpq_dsn(url))
    try:
        conn.autocommit = True
        yield Tx(conn)
    finally:
        _close_quietly(conn)

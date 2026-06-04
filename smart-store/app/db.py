"""Storage layer: SQLite (system of record) + Redis (live counters + stream).

SQLite holds events and POS rows in WAL mode for concurrent reads.
Redis holds per-store live counters powering the dashboard:
    visitors_today:{store_id}      (HyperLogLog of visitor_id)
    queue_depth:{store_id}         (int)
    last_event_ts:{store_id}       (ISO-8601 string)
    stream "events:{store_id}"     — Redis Stream of ingested events. Capped at
                                     ~10k entries via XADD MAXLEN ~ to bound
                                     memory; WS bridge tails it with XREAD and
                                     can replay from a client-supplied last_id
                                     after a reconnect (pub/sub silently dropped
                                     events for any client offline mid-flow).

The DB layer fails closed: every public coroutine that touches storage raises
StorageUnavailable on connection failure so the API can return a clean 503.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite
import redis.asyncio as redis

from .config import get_settings

log = logging.getLogger(__name__)


class StorageUnavailable(RuntimeError):
    """Raised when SQLite or Redis is unreachable. API maps this to HTTP 503."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL,
    camera_id    TEXT NOT NULL,
    visitor_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    ts           TEXT NOT NULL,         -- ISO-8601 UTC
    zone_id      TEXT,
    dwell_ms     INTEGER NOT NULL DEFAULT 0,
    is_staff     INTEGER NOT NULL DEFAULT 0,
    confidence   REAL NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    received_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_events_store_ts        ON events(store_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_store_visitor   ON events(store_id, visitor_id);
CREATE INDEX IF NOT EXISTS ix_events_type            ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_store_zone_ts   ON events(store_id, zone_id, ts);

CREATE TABLE IF NOT EXISTS pos_transactions (
    order_id      INTEGER PRIMARY KEY,
    store_id      TEXT NOT NULL,
    ts            TEXT NOT NULL,         -- ISO-8601 UTC
    product_id    INTEGER,
    brand_name    TEXT,
    total_amount  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_pos_store_ts ON pos_transactions(store_id, ts);
"""


class Database:
    """Wraps an aiosqlite connection. Singleton-per-process, opened in lifespan."""

    _instance: Optional["Database"] = None
    _path: str = ""
    _lock: asyncio.Lock = asyncio.Lock()
    _conn: Optional[aiosqlite.Connection] = None

    @classmethod
    async def open(cls, path: str) -> "Database":
        if cls._instance is None:
            inst = cls()
            cls._path = path
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            inst._conn = await aiosqlite.connect(path)
            inst._conn.row_factory = aiosqlite.Row
            await inst._conn.execute("PRAGMA journal_mode=WAL;")
            await inst._conn.execute("PRAGMA synchronous=NORMAL;")
            await inst._conn.executescript(SCHEMA)
            await inst._conn.commit()
            cls._instance = inst
            log.info("sqlite.opened path=%s", path)
        return cls._instance

    @classmethod
    def instance(cls) -> "Database":
        if cls._instance is None or cls._instance._conn is None:
            raise StorageUnavailable("Database is not open")
        return cls._instance

    @classmethod
    async def close(cls) -> None:
        if cls._instance and cls._instance._conn:
            await cls._instance._conn.close()
            cls._instance = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[aiosqlite.Cursor]:
        if self._conn is None:
            raise StorageUnavailable("Database connection is closed")
        async with self._lock:
            cur = await self._conn.cursor()
            try:
                yield cur
                await self._conn.commit()
            except Exception:
                await self._conn.rollback()
                raise
            finally:
                await cur.close()

    async def execute(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class RedisClient:
    """Thin wrapper providing counters + a per-store event stream. Falls back
    to a degraded mode if Redis is unreachable (dashboard still works against
    SQLite, just stale and without live updates)."""

    # Cap each per-store stream at ~10k recent events. XADD MAXLEN ~ trims
    # opportunistically (cheap), so the bound is approximate but bounded.
    EVENT_STREAM_MAXLEN = 10_000

    _client: Optional[redis.Redis] = None
    _ok: bool = False

    @classmethod
    async def open(cls, url: str) -> "RedisClient":
        try:
            # Set max_connections high enough that ingestion bursts + the
            # dashboard's pubsub subscriber can coexist without pool exhaustion.
            client = redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                max_connections=64,
            )
            await client.ping()
            cls._client = client
            cls._ok = True
            log.info("redis.connected url=%s", url)
        except Exception as e:  # noqa: BLE001
            log.warning("redis.unavailable url=%s err=%s — degraded mode", url, e)
            cls._client = None
            cls._ok = False
        return cls()

    @classmethod
    def is_ok(cls) -> bool:
        return cls._ok and cls._client is not None

    @classmethod
    async def close(cls) -> None:
        if cls._client:
            await cls._client.close()
            cls._client = None
            cls._ok = False

    # Counter operations -----------------------------------------------------
    # Every public method swallows transient Redis errors (connection-pool
    # exhaustion, timeouts) and returns the safe default. The system of record
    # is SQLite; Redis is an accelerator, never the source of truth.

    @classmethod
    async def add_visitor(cls, store_id: str, visitor_id: str, day_iso: str) -> None:
        if not cls.is_ok():
            return
        try:
            await cls._client.pfadd(f"visitors:{store_id}:{day_iso}", visitor_id)
            await cls._client.expire(f"visitors:{store_id}:{day_iso}", 60 * 60 * 36)
        except Exception as e:  # noqa: BLE001
            log.warning("redis.add_visitor_failed store=%s err=%s", store_id, e)

    @classmethod
    async def get_unique_visitors(cls, store_id: str, day_iso: str) -> int:
        if not cls.is_ok():
            return 0
        try:
            return int(await cls._client.pfcount(f"visitors:{store_id}:{day_iso}") or 0)
        except Exception as e:  # noqa: BLE001
            log.warning("redis.pfcount_failed store=%s err=%s", store_id, e)
            return 0

    @classmethod
    async def set_queue_depth(cls, store_id: str, depth: int) -> None:
        if not cls.is_ok():
            return
        try:
            await cls._client.set(f"queue_depth:{store_id}", depth, ex=600)
        except Exception as e:  # noqa: BLE001
            log.warning("redis.set_queue_depth_failed store=%s err=%s", store_id, e)

    @classmethod
    async def get_queue_depth(cls, store_id: str) -> int:
        if not cls.is_ok():
            return 0
        try:
            v = await cls._client.get(f"queue_depth:{store_id}")
            return int(v) if v is not None else 0
        except Exception as e:  # noqa: BLE001
            log.warning("redis.get_queue_depth_failed store=%s err=%s", store_id, e)
            return 0

    @classmethod
    async def set_last_event_ts(cls, store_id: str, ts: str) -> None:
        if not cls.is_ok():
            return
        try:
            await cls._client.set(f"last_event_ts:{store_id}", ts)
        except Exception as e:  # noqa: BLE001
            log.warning("redis.set_last_event_ts_failed store=%s err=%s", store_id, e)

    @classmethod
    async def get_last_event_ts(cls, store_id: str) -> Optional[str]:
        if not cls.is_ok():
            return None
        try:
            return await cls._client.get(f"last_event_ts:{store_id}")
        except Exception as e:  # noqa: BLE001
            log.warning("redis.get_last_event_ts_failed store=%s err=%s", store_id, e)
            return None

    # Event stream -----------------------------------------------------------

    @classmethod
    def event_stream_key(cls, store_id: str) -> str:
        return f"events:{store_id}"

    @classmethod
    async def publish_event(cls, store_id: str, payload: dict) -> None:
        """Append an event to the per-store stream. Method name preserved for
        backwards compatibility with the ingestion call site; the underlying
        operation is XADD with an approximate MAXLEN cap."""
        if not cls.is_ok():
            return
        try:
            await cls._client.xadd(
                cls.event_stream_key(store_id),
                {"event": json.dumps(payload, default=str)},
                maxlen=cls.EVENT_STREAM_MAXLEN,
                approximate=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("redis.xadd failed store=%s err=%s", store_id, e)

    @classmethod
    async def read_event_stream(
        cls,
        store_id: str,
        *,
        last_id: str = "$",
        block_ms: int = 2000,
        count: int = 100,
    ) -> list[tuple[str, dict]]:
        """Tail the per-store event stream. Returns a list of
        (entry_id, payload_dict) for every entry past `last_id`. `last_id="$"`
        starts from the moment the call is made; an explicit id replays from
        that point onward (useful when a WS client reconnects with a last_id
        cookie and wants to catch up without missing events).

        Returns an empty list on timeout or transient redis error.
        """
        if not cls.is_ok():
            return []
        try:
            res = await cls._client.xread(
                streams={cls.event_stream_key(store_id): last_id},
                count=count,
                block=block_ms,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("redis.xread failed store=%s err=%s", store_id, e)
            return []
        out: list[tuple[str, dict]] = []
        for _stream_key, entries in res or []:
            for entry_id, fields in entries:
                raw = fields.get("event") if isinstance(fields, dict) else None
                if raw is None:
                    continue
                try:
                    out.append((entry_id, json.loads(raw)))
                except Exception:  # noqa: BLE001
                    continue
        return out


# ---------------------------------------------------------------------------
# Module-level helpers used by the API lifespan
# ---------------------------------------------------------------------------


async def init_storage() -> None:
    settings = get_settings()
    # ensure parent dir exists for SQLite path
    p = Path(settings.sqlite_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    await Database.open(settings.sqlite_path)
    await RedisClient.open(settings.redis_url)


async def shutdown_storage() -> None:
    await Database.close()
    await RedisClient.close()

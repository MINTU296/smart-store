"""GET /health — service liveness + per-store data freshness.

Critical for on-call: returns
    status        : "ok" | "degraded"
    db_ok         : SQLite reachable
    redis_ok      : Redis reachable (cached counters work)
    stores[]      : per-store last_event_ts + stale flag
    warnings[]    : human-readable hints

stale = (now - last_event_ts) > STALE_FEED_MINUTES
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter

from .config import get_settings
from .db import Database, RedisClient
from .models import HealthResponse, StoreHealth

router = APIRouter(tags=["health"])


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    warnings: list[str] = []

    db_ok = True
    stores: list[StoreHealth] = []
    try:
        db = Database.instance()
        rows = await db.execute(
            "SELECT store_id, MAX(ts) AS last_ts FROM events GROUP BY store_id"
        )
        now = datetime.now(timezone.utc)
        cutoff = timedelta(minutes=settings.stale_feed_minutes)
        for r in rows:
            last = _parse(r["last_ts"])
            stale = bool(last and (now - last) > cutoff)
            stores.append(StoreHealth(store_id=r["store_id"], last_event_ts=r["last_ts"], stale=stale))
            if stale:
                warnings.append(f"STALE_FEED store_id={r['store_id']} last={r['last_ts']}")
    except Exception:
        db_ok = False
        warnings.append("DB_UNAVAILABLE")

    redis_ok = RedisClient.is_ok()
    if not redis_ok:
        warnings.append("REDIS_UNAVAILABLE: live counters degraded; SQLite still authoritative.")

    status = "ok" if (db_ok and not any(s.stale for s in stores)) else "degraded"
    return HealthResponse(
        status=status, db_ok=db_ok, redis_ok=redis_ok, stores=stores, warnings=warnings
    )

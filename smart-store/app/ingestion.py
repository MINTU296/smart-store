"""POST /events/ingest — batch event ingestion.

Contract:
- Accepts {"events": [...]} with up to 500 events.
- Validates each event individually. Per-event status returned: stored / duplicate / rejected.
- Idempotent by event_id (PRIMARY KEY): re-posting the same payload yields
  duplicates and zero new rows.
- Side-effects (best-effort, never fail the request): Redis counter updates and
  pub/sub broadcast for the live dashboard.
- Storage errors (DB unreachable) -> 503 with structured body.

The schema follows the PDF spec; the sample_events.jsonl in /data is illustrative
only and is intentionally not used as a contract.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from .db import Database, RedisClient, StorageUnavailable
from .models import Event, EventResult, IngestRequest, IngestResponse

router = APIRouter(prefix="/events", tags=["events"])
log = logging.getLogger("api.ingest")


def _day_bucket(ts_iso: str) -> str:
    """YYYY-MM-DD bucket for visitor uniqueness counters."""
    try:
        # tolerate trailing Z or microseconds
        ts = ts_iso.rstrip("Z")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


async def _store_one(cur, e: Event) -> str:
    """Insert a single event. Returns 'stored' or 'duplicate'."""
    try:
        await cur.execute(
            """
            INSERT INTO events
              (event_id, store_id, camera_id, visitor_id, event_type, ts,
               zone_id, dwell_ms, is_staff, confidence, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                e.event_id,
                e.store_id,
                e.camera_id,
                e.visitor_id,
                e.event_type.value,
                e.timestamp,
                e.zone_id,
                e.dwell_ms,
                1 if e.is_staff else 0,
                e.confidence,
                json.dumps(e.metadata.model_dump(exclude_none=False)),
            ),
        )
        return "stored"
    except Exception as exc:  # likely UNIQUE constraint
        if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
            return "duplicate"
        raise


async def _side_effects(e: Event) -> None:
    """Redis counters + dashboard pub/sub. Best-effort; logs but never raises."""
    try:
        if not e.is_staff:
            await RedisClient.add_visitor(e.store_id, e.visitor_id, _day_bucket(e.timestamp))
        if e.event_type.value == "BILLING_QUEUE_JOIN" and e.metadata.queue_depth is not None:
            await RedisClient.set_queue_depth(e.store_id, int(e.metadata.queue_depth))
        await RedisClient.set_last_event_ts(e.store_id, e.timestamp)
        await RedisClient.publish_event(
            e.store_id,
            {
                "event_id": e.event_id,
                "store_id": e.store_id,
                "event_type": e.event_type.value,
                "visitor_id": e.visitor_id,
                "ts": e.timestamp,
                "zone_id": e.zone_id,
                "queue_depth": e.metadata.queue_depth,
                "is_staff": e.is_staff,
            },
        )
    except Exception as ex:  # noqa: BLE001
        log.warning("ingest.side_effect_failed event_id=%s err=%s", e.event_id, ex)


@router.post("/ingest", response_model=IngestResponse)
async def ingest(payload: dict[str, Any], request: Request) -> IngestResponse:
    """Ingest a batch of up to 500 events. Idempotent by event_id."""
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "MALFORMED_PAYLOAD", "message": "Expected {'events': [...]}"},
        )
    if len(raw_events) > 500:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "BATCH_TOO_LARGE", "message": "Max 500 events per batch"},
        )

    request.state.event_count = len(raw_events)

    # Validate each event individually so partial-success works
    parsed: list[tuple[int, Event] | tuple[int, str]] = []
    for idx, raw in enumerate(raw_events):
        try:
            parsed.append((idx, Event.model_validate(raw)))
        except ValidationError as ve:
            parsed.append((idx, str(ve.errors()[0]["msg"]) if ve.errors() else "validation_error"))

    try:
        db = Database.instance()
    except StorageUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB_UNAVAILABLE", "retry_after": 5, "message": str(e)},
        )

    results: list[EventResult] = []
    accepted = duplicates = rejected = 0
    successful_events: list[Event] = []

    try:
        async with db.cursor() as cur:
            for idx, item in parsed:
                if isinstance(item, str):
                    rejected += 1
                    raw_id = (
                        raw_events[idx].get("event_id", f"_idx_{idx}")
                        if isinstance(raw_events[idx], dict)
                        else f"_idx_{idx}"
                    )
                    results.append(EventResult(event_id=raw_id, status="rejected", error=item))
                    continue
                event = item
                try:
                    outcome = await _store_one(cur, event)
                except Exception as ex:  # noqa: BLE001
                    rejected += 1
                    results.append(
                        EventResult(event_id=event.event_id, status="rejected", error=str(ex))
                    )
                    continue
                if outcome == "stored":
                    accepted += 1
                    successful_events.append(event)
                else:
                    duplicates += 1
                results.append(EventResult(event_id=event.event_id, status=outcome))
    except StorageUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB_UNAVAILABLE", "retry_after": 5, "message": str(e)},
        )

    for e in successful_events:
        await _side_effects(e)

    return IngestResponse(
        accepted=accepted, duplicates=duplicates, rejected=rejected, results=results
    )

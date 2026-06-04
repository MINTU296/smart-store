"""WebSocket bridge: dashboard ⇄ Redis Streams.

Each connected client subscribes to /ws/{store_id}. Server tails the
events:{store_id} stream via XREAD and forwards every entry's event JSON to
the client. If Redis is unavailable the WS still connects and sends a synthetic
"degraded" notice plus a periodic snapshot so the dashboard renders sensibly.

Streams (vs the previous pub/sub implementation) give us replay-on-reconnect:
the WS sends the most recent stream entry id with every event, and clients can
reconnect with `?last_id=<id>` to catch up on whatever they missed while
offline. `last_id="$"` (the default) tails only fresh events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from .db import RedisClient
from .metrics import metrics as metrics_endpoint

router = APIRouter(tags=["ws"])
log = logging.getLogger("api.ws")
# Reuse the same logger/structure the HTTP RequestLogMiddleware uses so WS
# snapshots don't bypass the observability spec ("Every request logs trace_id,
# store_id, endpoint, latency_ms, event_count, status_code"). Without this,
# 100 connected dashboard clients silently emit zero log lines.
_api_log = logging.getLogger("api")


async def _send_metrics_snapshot(ws: WebSocket, store_id: str, trace_id: str) -> None:
    start = time.perf_counter()
    status_code = 200
    try:
        snap = await metrics_endpoint(store_id)
        await ws.send_text(json.dumps({"type": "snapshot", "data": snap.model_dump()}))
    except Exception as e:  # noqa: BLE001
        status_code = 500
        log.warning("ws.snapshot_failed store=%s err=%s", store_id, e)
    finally:
        _api_log.info(
            "request",
            extra={
                "trace_id": trace_id,
                "store_id": store_id,
                "endpoint": "ws.snapshot",
                "method": "WS",
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "event_count": None,
                "status_code": status_code,
            },
        )


@router.websocket("/ws/{store_id}")
async def ws(
    ws: WebSocket,
    store_id: str,
    last_id: str = Query(
        "$",
        description=(
            "Optional Redis Stream id to resume from. '$' (default) tails only "
            "events that arrive after the connection. Pass the last entry id "
            "the client received to replay any events missed while offline."
        ),
    ),
) -> None:
    await ws.accept()
    # One trace_id per WS connection — every snapshot for this connection
    # shares it so structured-log correlation works across the connection's
    # lifetime, mirroring the request middleware's per-HTTP-call behaviour.
    trace_id = uuid.uuid4().hex[:12]
    await _send_metrics_snapshot(ws, store_id, trace_id)

    if not RedisClient.is_ok():
        await ws.send_text(
            json.dumps(
                {
                    "type": "degraded",
                    "reason": "redis_unavailable",
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        try:
            while True:
                await asyncio.sleep(5)
                await _send_metrics_snapshot(ws, store_id, trace_id)
        except WebSocketDisconnect:
            return

    cursor = last_id
    # On a fresh connection (last_id="$"), seed the live-stream panel with the
    # 20 most recent events so the user sees activity immediately rather than
    # an empty "waiting for events…" state. After the seed, advance the cursor
    # to the latest stream id so we don't double-emit when XREAD wakes up.
    if last_id == "$":
        recent = await RedisClient.read_recent_events(store_id, count=20)
        for entry_id, payload in recent:
            cursor = entry_id
            await ws.send_text(
                json.dumps(
                    {"type": "event", "stream_id": entry_id, "data": payload}
                )
            )
    last_snapshot = datetime.now(timezone.utc)
    try:
        while True:
            entries = await RedisClient.read_event_stream(
                store_id, last_id=cursor, block_ms=2000, count=100
            )
            for entry_id, payload in entries:
                cursor = entry_id
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "event",
                            "stream_id": entry_id,
                            "data": payload,
                        }
                    )
                )
            now = datetime.now(timezone.utc)
            if (now - last_snapshot).total_seconds() >= 10:
                await _send_metrics_snapshot(ws, store_id, trace_id)
                last_snapshot = now
    except WebSocketDisconnect:
        return

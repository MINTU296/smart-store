# PROMPT: "Write tests for the POS-correlation logic in app/pos.py: visitors_who_purchased
#   should match a billing-zone visitor with a POS row in the next 5min, but exclude one
#   whose POS row is 7min later."
# CHANGES MADE:
#   - Originally I had a single test, but the off-by-one at exactly 5min was missed.
#     Added an explicit boundary test (exactly window_sec → still counted).
#   - Python 3.14 removed the implicit event-loop fallback in asyncio.get_event_loop();
#     replaced with an explicit asyncio.new_event_loop() per test so the suite runs on
#     both 3.12 and 3.14.
from __future__ import annotations

import asyncio

from app.db import Database
from app.pos import visitors_who_purchased

STORE = "STORE_BLR_POS"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _e(eid, vid, etype, ts, zone=None, metadata=None):
    return {
        "event_id": eid, "store_id": STORE, "camera_id": "CAM_BILLING",
        "visitor_id": vid, "event_type": etype, "timestamp": ts,
        "zone_id": zone, "dwell_ms": 0, "is_staff": False,
        "confidence": 0.9, "metadata": metadata or {},
    }


async def _insert_pos(order_id, ts):
    async with Database.instance().cursor() as cur:
        await cur.execute(
            "INSERT INTO pos_transactions (order_id, store_id, ts, total_amount) VALUES (?, ?, ?, ?)",
            (order_id, STORE, ts, 100.0),
        )


def test_correlation_within_window(client):
    client.post("/events/ingest", json={"events": [
        _e("p1", "V1", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _run(_insert_pos(990001, "2026-03-08T11:03:00Z"))
    converted = _run(visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z"))
    assert "V1" in converted


def test_correlation_outside_window(client):
    client.post("/events/ingest", json={"events": [
        _e("p2", "V2", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _run(_insert_pos(990002, "2026-03-08T11:07:00Z"))
    converted = _run(visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z", window_sec=300))
    assert "V2" not in converted


def test_correlation_at_window_boundary(client):
    client.post("/events/ingest", json={"events": [
        _e("p3", "V3", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _run(_insert_pos(990003, "2026-03-08T11:05:00Z"))  # exactly 5min
    converted = _run(visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z", window_sec=300))
    assert "V3" in converted, "exactly window_sec must count as inside"

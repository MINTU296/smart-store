#!/usr/bin/env python3
"""Emit a JSON payload for the smoke test.

The event_ids are deterministic (uuid5 over the tuple), but timestamps are
anchored to *now* so the events land inside the API's "today" window. That
combination keeps the smoke test reproducible while still proving that
outputs vary with input (integrity-check defence).

The 10-event sequence walks one visitor through the funnel
(ENTRY → ZONE_ENTER → ZONE_DWELL → BILLING_QUEUE_JOIN), one re-entrant, one
staff member, and one deliberately malformed event so partial-success is
visible in the response.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

NS = uuid.UUID("e3a5c1f0-1111-4321-8888-deadbeefcafe")  # smoke fixture namespace


def eid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "|".join(parts)))


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build(store: str) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cam_entry = "CAM_ENTRY"
    cam_floor = "CAM_FLOOR_1"
    cam_billing = "CAM_BILLING"
    v1 = "VIS_smoke_v1"
    v2 = "VIS_smoke_v2"
    v_staff = "VIS_smoke_staff"
    zone = "MOISTURISER"

    events = [
        # v1 walks the full funnel
        {
            "event_id": eid(store, cam_entry, v1, "ENTRY", iso(now - timedelta(minutes=12))),
            "store_id": store, "camera_id": cam_entry, "visitor_id": v1,
            "event_type": "ENTRY", "timestamp": iso(now - timedelta(minutes=12)),
            "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.92,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        },
        {
            "event_id": eid(store, cam_floor, v1, "ZONE_ENTER", iso(now - timedelta(minutes=11))),
            "store_id": store, "camera_id": cam_floor, "visitor_id": v1,
            "event_type": "ZONE_ENTER", "timestamp": iso(now - timedelta(minutes=11)),
            "zone_id": zone, "dwell_ms": 0, "is_staff": False, "confidence": 0.88,
            "metadata": {"queue_depth": None, "sku_zone": zone, "session_seq": 2},
        },
        {
            "event_id": eid(store, cam_floor, v1, "ZONE_DWELL", iso(now - timedelta(minutes=10))),
            "store_id": store, "camera_id": cam_floor, "visitor_id": v1,
            "event_type": "ZONE_DWELL", "timestamp": iso(now - timedelta(minutes=10)),
            "zone_id": zone, "dwell_ms": 60_000, "is_staff": False, "confidence": 0.85,
            "metadata": {"queue_depth": None, "sku_zone": zone, "session_seq": 3},
        },
        {
            "event_id": eid(store, cam_billing, v1, "BILLING_QUEUE_JOIN", iso(now - timedelta(minutes=8))),
            "store_id": store, "camera_id": cam_billing, "visitor_id": v1,
            "event_type": "BILLING_QUEUE_JOIN", "timestamp": iso(now - timedelta(minutes=8)),
            "zone_id": "BILLING", "dwell_ms": 0, "is_staff": False, "confidence": 0.90,
            "metadata": {"queue_depth": 3, "sku_zone": None, "session_seq": 4},
        },
        # v2 enters and re-enters
        {
            "event_id": eid(store, cam_entry, v2, "ENTRY", iso(now - timedelta(minutes=20))),
            "store_id": store, "camera_id": cam_entry, "visitor_id": v2,
            "event_type": "ENTRY", "timestamp": iso(now - timedelta(minutes=20)),
            "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.91,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        },
        {
            "event_id": eid(store, cam_entry, v2, "EXIT", iso(now - timedelta(minutes=18))),
            "store_id": store, "camera_id": cam_entry, "visitor_id": v2,
            "event_type": "EXIT", "timestamp": iso(now - timedelta(minutes=18)),
            "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.89,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 2},
        },
        {
            "event_id": eid(store, cam_entry, v2, "REENTRY", iso(now - timedelta(minutes=15))),
            "store_id": store, "camera_id": cam_entry, "visitor_id": v2,
            "event_type": "REENTRY", "timestamp": iso(now - timedelta(minutes=15)),
            "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.87,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 3},
        },
        # staff (excluded from customer counts)
        {
            "event_id": eid(store, cam_entry, v_staff, "ENTRY", iso(now - timedelta(minutes=30))),
            "store_id": store, "camera_id": cam_entry, "visitor_id": v_staff,
            "event_type": "ENTRY", "timestamp": iso(now - timedelta(minutes=30)),
            "zone_id": None, "dwell_ms": 0, "is_staff": True, "confidence": 0.93,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        },
        # ambient ZONE_ENTER for v1 (extra zone for heatmap signal)
        {
            "event_id": eid(store, cam_floor, v1, "ZONE_ENTER", iso(now - timedelta(minutes=11, seconds=30))),
            "store_id": store, "camera_id": cam_floor, "visitor_id": v1,
            "event_type": "ZONE_ENTER", "timestamp": iso(now - timedelta(minutes=11, seconds=30)),
            "zone_id": "FRAGRANCE", "dwell_ms": 0, "is_staff": False, "confidence": 0.83,
            "metadata": {"queue_depth": None, "sku_zone": "FRAGRANCE", "session_seq": 2},
        },
        # malformed — confidence > 1.0 trips Pydantic, demonstrates rejected bucket
        {
            "event_id": "smoke-malformed-001",
            "store_id": store, "camera_id": cam_entry, "visitor_id": "VIS_smoke_bad",
            "event_type": "ENTRY", "timestamp": iso(now - timedelta(minutes=5)),
            "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 7.5,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
        },
    ]
    return {"events": events}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--store", default="STORE_BLR_001")
    args = p.parse_args(argv)
    json.dump(build(args.store), sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

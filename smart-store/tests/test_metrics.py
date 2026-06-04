# PROMPT: "Write tests for /stores/{id}/metrics that cover (a) empty store returns 0/0/0
#   without crashing, (b) all-staff clip excludes staff visitors from unique_visitors,
#   (c) zero purchases yields conversion_rate=0.0, (d) a billing-zone visitor with a
#   POS row in the next 5min counts as converted."
# CHANGES MADE:
#   - The AI used datetime.now() for the POS rows and the events; that drifted between
#     the request time and "today_window", causing flakey assertions. I switched everything
#     to fixed ISO timestamps in 2026 to match the supplied data.
#   - The all-staff test originally asserted unique_visitors == 0 but missed the
#     conversion_rate==0.0 invariant for that case. Added it.
#   - Added test_abandonment_rate_pos_corrected (B-2): a JOIN+ABANDON visitor
#     who has a POS row 30s later must NOT count as abandoned. Spec §3.3 says
#     ABANDON requires POS correlation — counting raw events overstates churn.
from __future__ import annotations

import csv
from pathlib import Path

import pytest

STORE = "STORE_BLR_TEST"


def _post(client, events):
    return client.post("/events/ingest", json={"events": events})


def _e(**kw):
    base = {
        "event_id": kw["event_id"],
        "store_id": STORE,
        "camera_id": kw.get("camera_id", "CAM_ENTRY"),
        "visitor_id": kw["visitor_id"],
        "event_type": kw["event_type"],
        "timestamp": kw["timestamp"],
        "zone_id": kw.get("zone_id"),
        "dwell_ms": kw.get("dwell_ms", 0),
        "is_staff": kw.get("is_staff", False),
        "confidence": kw.get("confidence", 0.9),
        "metadata": kw.get("metadata", {}),
    }
    return base


def test_metrics_empty_store(client):
    r = client.get(f"/stores/EMPTY_STORE/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["has_data"] is False


def test_metrics_excludes_staff(client):
    # 1 staff entry + 2 customers
    events = [
        _e(event_id="aa11", visitor_id="VIS_staff",  event_type="ENTRY", is_staff=True,  timestamp="2026-03-08T10:00:00Z"),
        _e(event_id="aa12", visitor_id="VIS_cust1",  event_type="ENTRY", is_staff=False, timestamp="2026-03-08T10:01:00Z"),
        _e(event_id="aa13", visitor_id="VIS_cust2",  event_type="ENTRY", is_staff=False, timestamp="2026-03-08T10:02:00Z"),
    ]
    _post(client, events)
    body = client.get(f"/stores/{STORE}/metrics").json()
    assert body["unique_visitors"] == 2  # staff excluded
    assert body["conversion_rate"] == 0.0  # no POS rows


def test_metrics_zero_purchases(client):
    events = [
        _e(event_id="bb01", visitor_id="VIS_cust1", event_type="ENTRY", timestamp="2026-03-08T10:00:00Z"),
        _e(event_id="bb02", visitor_id="VIS_cust1", event_type="BILLING_QUEUE_JOIN",
           zone_id="BILLING", metadata={"queue_depth": 1}, timestamp="2026-03-08T10:05:00Z"),
    ]
    _post(client, events)
    body = client.get(f"/stores/{STORE}/metrics").json()
    assert body["unique_visitors"] == 1
    assert body["purchasing_visitors"] == 0
    assert body["conversion_rate"] == 0.0


def _insert_pos_sync(order_id: int, ts: str, total: float) -> None:
    """Synchronous POS insert via sqlite3 (M-7: avoids the new-event-loop
    bridge that was racing aiosqlite's connection-loop binding)."""
    import os
    import sqlite3

    with sqlite3.connect(os.environ["SQLITE_PATH"], timeout=2.0) as con:
        con.execute(
            "INSERT INTO pos_transactions (order_id, store_id, ts, total_amount) "
            "VALUES (?, ?, ?, ?)",
            (order_id, STORE, ts, total),
        )
        con.commit()


def test_metrics_with_pos_correlation(client, tmp_path):
    """A visitor in the billing zone within 5 min before a POS tx counts as converted."""
    _insert_pos_sync(999_001, "2026-03-08T10:08:00Z", 1500.0)

    events = [
        _e(event_id="cc01", visitor_id="VIS_buyer", event_type="ENTRY", timestamp="2026-03-08T10:00:00Z"),
        _e(event_id="cc02", visitor_id="VIS_buyer", event_type="BILLING_QUEUE_JOIN",
           zone_id="BILLING", metadata={"queue_depth": 1}, timestamp="2026-03-08T10:06:00Z"),
    ]
    _post(client, events)
    body = client.get(f"/stores/{STORE}/metrics").json()
    assert body["purchasing_visitors"] == 1
    assert body["conversion_rate"] == 1.0


def test_abandonment_rate_pos_corrected(client, tmp_path):
    """B-2: a visitor who emitted JOIN+ABANDON but later paid via POS must NOT
    count as abandoned. The pipeline emits ABANDON whenever the visitor leaves
    the queue polygon for >5s — but a brief step-out followed by a real
    purchase is not a churn event. Spec §3.3 says ABANDON requires POS
    correlation; we resolve at read time.
    """
    # POS tx 30s after VIS_paid's BILLING_QUEUE_ABANDON.
    _insert_pos_sync(999_002, "2026-03-08T10:08:30Z", 1200.0)

    events = [
        # Two visitors: one truly abandoned, one a brief step-out who then paid.
        _e(event_id="ab01", visitor_id="VIS_paid", event_type="ENTRY",
           timestamp="2026-03-08T10:00:00Z"),
        _e(event_id="ab02", visitor_id="VIS_paid", event_type="BILLING_QUEUE_JOIN",
           zone_id="BILLING", metadata={"queue_depth": 2}, timestamp="2026-03-08T10:07:00Z"),
        _e(event_id="ab03", visitor_id="VIS_paid", event_type="BILLING_QUEUE_ABANDON",
           zone_id="BILLING", metadata={"queue_depth": 2}, timestamp="2026-03-08T10:08:00Z"),
        _e(event_id="ab04", visitor_id="VIS_lost", event_type="ENTRY",
           timestamp="2026-03-08T10:10:00Z"),
        _e(event_id="ab05", visitor_id="VIS_lost", event_type="BILLING_QUEUE_JOIN",
           zone_id="BILLING", metadata={"queue_depth": 1}, timestamp="2026-03-08T10:12:00Z"),
        _e(event_id="ab06", visitor_id="VIS_lost", event_type="BILLING_QUEUE_ABANDON",
           zone_id="BILLING", metadata={"queue_depth": 1}, timestamp="2026-03-08T10:14:00Z"),
    ]
    _post(client, events)
    body = client.get(f"/stores/{STORE}/metrics").json()
    # 2 visitors joined; 1 paid (POS-correlated), 1 truly abandoned → rate 0.5
    assert body["purchasing_visitors"] == 1
    assert body["abandonment_rate"] == 0.5, (
        f"expected POS-corrected rate=0.5 (1 of 2 truly abandoned), "
        f"got {body['abandonment_rate']}"
    )

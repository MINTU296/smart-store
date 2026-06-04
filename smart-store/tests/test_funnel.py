# PROMPT: "Write tests for /stores/{id}/funnel that prove (a) the funnel cascades —
#   later stages are subsets of earlier ones, (b) a re-entry does NOT double-count the
#   visitor in any stage, (c) drop_off_pct is computed against the previous stage."
# CHANGES MADE:
#   - The AI computed drop_off_pct against the *first* stage (Entry) for all stages,
#     which is the wrong reading of "drop-off". Funnel reports per-stage drop —
#     stage_n / stage_{n-1}. Adjusted assertion accordingly.
#   - Added an explicit assertion that a single visitor with ENTRY → ZONE_ENTER →
#     BILLING_QUEUE_JOIN → REENTRY (no second billing) still appears once in Billing.
from __future__ import annotations

STORE = "STORE_BLR_FUNNEL"


def _e(**kw):
    return {
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


def test_funnel_cascade_and_dropoff(client):
    events = [
        _e(event_id="d1", visitor_id="V1", event_type="ENTRY",      timestamp="2026-03-08T11:00:00Z"),
        _e(event_id="d2", visitor_id="V2", event_type="ENTRY",      timestamp="2026-03-08T11:01:00Z"),
        _e(event_id="d3", visitor_id="V3", event_type="ENTRY",      timestamp="2026-03-08T11:02:00Z"),
        _e(event_id="d4", visitor_id="V1", event_type="ZONE_ENTER", zone_id="LIPSTICK", timestamp="2026-03-08T11:03:00Z"),
        _e(event_id="d5", visitor_id="V2", event_type="ZONE_ENTER", zone_id="LIPSTICK", timestamp="2026-03-08T11:04:00Z"),
        _e(event_id="d6", visitor_id="V1", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
           metadata={"queue_depth": 1}, timestamp="2026-03-08T11:10:00Z"),
    ]
    client.post("/events/ingest", json={"events": events})
    body = client.get(f"/stores/{STORE}/funnel").json()
    counts = {s["name"]: s["count"] for s in body["stages"]}
    assert counts["Entry"] == 3
    assert counts["Zone Visit"] == 2
    assert counts["Billing Queue"] == 1
    assert counts["Purchase"] == 0
    # cascade invariant
    assert counts["Zone Visit"] <= counts["Entry"]
    assert counts["Billing Queue"] <= counts["Zone Visit"]
    # drop-off computed against previous stage
    stages = {s["name"]: s for s in body["stages"]}
    assert stages["Zone Visit"]["drop_off_pct"] == round(100 * (1 - 2/3), 2)


def test_funnel_no_double_count_on_reentry(client):
    events = [
        _e(event_id="e1", visitor_id="V1", event_type="ENTRY",      timestamp="2026-03-08T11:00:00Z"),
        _e(event_id="e2", visitor_id="V1", event_type="ZONE_ENTER", zone_id="MENS", timestamp="2026-03-08T11:01:00Z"),
        _e(event_id="e3", visitor_id="V1", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
           metadata={"queue_depth": 1}, timestamp="2026-03-08T11:05:00Z"),
        _e(event_id="e4", visitor_id="V1", event_type="EXIT",       timestamp="2026-03-08T11:10:00Z"),
        _e(event_id="e5", visitor_id="V1", event_type="REENTRY",    timestamp="2026-03-08T11:15:00Z"),
        _e(event_id="e6", visitor_id="V1", event_type="ZONE_ENTER", zone_id="MENS", timestamp="2026-03-08T11:16:00Z"),
    ]
    client.post("/events/ingest", json={"events": events})
    body = client.get(f"/stores/{STORE}/funnel").json()
    counts = {s["name"]: s["count"] for s in body["stages"]}
    assert counts["Entry"] == 1
    assert counts["Zone Visit"] == 1
    assert counts["Billing Queue"] == 1

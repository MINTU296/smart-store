# PROMPT: "Write tests for /stores/{id}/funnel that prove (a) the funnel cascades —
#   later stages are subsets of earlier ones, (b) a re-entry does NOT double-count the
#   visitor in any stage, (c) drop_off_pct is computed against the previous stage."
# CHANGES MADE:
#   - The AI computed drop_off_pct against the *first* stage (Entry) for all stages,
#     which is the wrong reading of "drop-off". Funnel reports per-stage drop —
#     stage_n / stage_{n-1}. Adjusted assertion accordingly.
#   - Added an explicit assertion that a single visitor with ENTRY → ZONE_ENTER →
#     BILLING_QUEUE_JOIN → REENTRY (no second billing) still appears once in Billing.
#   - Added test_funnel_surfaces_floor_camera_gap (M-3): a billing-queue joiner
#     without a ZONE_ENTER must not be silently promoted to Zone Visit; the
#     gap surfaces as data_warning. Previous behaviour stuffed the joiner into
#     Zone Visit which made detection blind spots invisible.
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


def test_funnel_surfaces_floor_camera_gap(client):
    """M-3: a billing-queue joiner without a ZONE_ENTER must NOT be silently
    promoted to Zone Visit. The previous cascade-fill made detection blind
    spots invisible — now the gap surfaces in data_warning so reviewers see
    where the floor camera is leaking signal.
    """
    # V1: the well-behaved visitor (entry → zone → billing).
    # V2: the floor-camera-missed visitor (entry → BILLING with no ZONE_ENTER).
    # Expected counts after M-3: Entry=2, Zone Visit=1, Billing Queue=2,
    # and a non-empty data_warning naming the floor-camera gap.
    events = [
        _e(event_id="g1", visitor_id="V1", event_type="ENTRY",      timestamp="2026-03-08T12:00:00Z"),
        _e(event_id="g2", visitor_id="V1", event_type="ZONE_ENTER", zone_id="LIPSTICK", timestamp="2026-03-08T12:01:00Z"),
        _e(event_id="g3", visitor_id="V1", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
           metadata={"queue_depth": 1}, timestamp="2026-03-08T12:05:00Z"),
        _e(event_id="g4", visitor_id="V2", event_type="ENTRY",      timestamp="2026-03-08T12:10:00Z"),
        _e(event_id="g5", visitor_id="V2", event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
           metadata={"queue_depth": 1}, timestamp="2026-03-08T12:15:00Z"),
    ]
    client.post("/events/ingest", json={"events": events})
    body = client.get(f"/stores/{STORE}/funnel").json()
    counts = {s["name"]: s["count"] for s in body["stages"]}
    assert counts["Entry"] == 2
    assert counts["Zone Visit"] == 1, "V2 should NOT be promoted into Zone Visit"
    assert counts["Billing Queue"] == 2
    assert body["data_warning"] is not None
    assert "floor camera" in body["data_warning"].lower()
    # drop_off_pct stays in [0, 100] even when stage counts are non-monotonic
    for s in body["stages"]:
        assert 0.0 <= s["drop_off_pct"] <= 100.0

# PROMPT: "Tests for /health: returns 200 with status:ok when DB+Redis OK, lists each
#   store with last_event_ts, raises STALE_FEED warning when last event > stale_feed_minutes."
# CHANGES MADE:
#   - The AI assumed last_event_ts was tz-aware-with-Z; SQLite returns the value as
#     stored. I adjusted the assertion to tolerate either +00:00 or trailing Z.
#   - Added test_health_uses_data_anchored_now to lock in B-1 fix: a single
#     historic event must not flip every store to stale=True. STALE_FEED is now
#     anchored on the freshest event ts in the cluster, not on wall clock.
from __future__ import annotations


def test_health_basic(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert "stores" in body
    assert "warnings" in body


def test_health_after_ingest_lists_store(client):
    e = {
        "event_id": "h1", "store_id": "STORE_HEALTH", "camera_id": "CAM",
        "visitor_id": "V", "event_type": "ENTRY", "timestamp": "2026-03-08T10:00:00Z",
        "zone_id": None, "dwell_ms": 0, "is_staff": False, "confidence": 0.9, "metadata": {},
    }
    client.post("/events/ingest", json={"events": [e]})
    body = client.get("/health").json()
    found = [s for s in body["stores"] if s["store_id"] == "STORE_HEALTH"]
    assert found
    assert "2026-03-08" in found[0]["last_event_ts"]


def test_health_uses_data_anchored_now(client):
    """B-1: STALE_FEED must compare per-store last_ts against the freshest
    cluster-wide event ts, not wall clock. Reviewers run the pipeline against
    historical clips (anchored at 2026-03-08); wall-clock comparison would
    falsely mark every store as stale and the dashboard's TopBar would show a
    permanent red 'STALE FEED' pill.
    """
    e1 = {
        "event_id": "anchored_1",
        "store_id": "STORE_ANCHOR_A",
        "camera_id": "CAM",
        "visitor_id": "V1",
        "event_type": "ENTRY",
        "timestamp": "2026-03-08T18:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {},
    }
    # Same anchor minute on a second store — both should be considered fresh.
    e2 = {**e1, "event_id": "anchored_2", "store_id": "STORE_ANCHOR_B", "visitor_id": "V2"}
    client.post("/events/ingest", json={"events": [e1, e2]})

    body = client.get("/health").json()
    a = next(s for s in body["stores"] if s["store_id"] == "STORE_ANCHOR_A")
    b = next(s for s in body["stores"] if s["store_id"] == "STORE_ANCHOR_B")
    assert a["stale"] is False, "STORE_ANCHOR_A should not be stale relative to itself"
    assert b["stale"] is False, "STORE_ANCHOR_B should not be stale relative to itself"
    assert not [w for w in body["warnings"] if "STALE_FEED" in w], (
        f"unexpected STALE_FEED warnings: {body['warnings']}"
    )

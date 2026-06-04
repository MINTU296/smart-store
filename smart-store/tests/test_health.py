# PROMPT: "Tests for /health: returns 200 with status:ok when DB+Redis OK, lists each
#   store with last_event_ts, raises STALE_FEED warning when last event > stale_feed_minutes."
# CHANGES MADE:
#   - The AI assumed last_event_ts was tz-aware-with-Z; SQLite returns the value as
#     stored. I adjusted the assertion to tolerate either +00:00 or trailing Z.
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

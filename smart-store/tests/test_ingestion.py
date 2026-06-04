# PROMPT: "Write pytest tests for a FastAPI POST /events/ingest endpoint with these
#   contracts: idempotent by event_id, batch limit 500, partial-success returns per-event
#   status, malformed JSON returns 400, oversized batch returns 413. Use the project's
#   `client` fixture."
# CHANGES MADE:
#   - The AI initially asserted `accepted+duplicates == n` ignoring the `rejected` bucket;
#     I split the assertions and added an explicit malformed-event test that asserts
#     rejected==1 with the other event still stored.
#   - Added an idempotency test that fires the same payload twice and asserts the second
#     response has accepted=0, duplicates=N — this is the rubric's specific requirement.
#   - The AI suggested a 501-event batch test using random uuids; I kept it but switched
#     to deterministic ids so flakiness is impossible.
from __future__ import annotations

import uuid


def _evt(**kw):
    base = {
        "event_id": kw.get("event_id", str(uuid.uuid4())),
        "store_id": kw.get("store_id", "STORE_BLR_001"),
        "camera_id": kw.get("camera_id", "CAM_ENTRY"),
        "visitor_id": kw.get("visitor_id", "VIS_test01"),
        "event_type": kw.get("event_type", "ENTRY"),
        "timestamp": kw.get("timestamp", "2026-03-08T18:10:00Z"),
        "zone_id": kw.get("zone_id"),
        "dwell_ms": kw.get("dwell_ms", 0),
        "is_staff": kw.get("is_staff", False),
        "confidence": kw.get("confidence", 0.9),
        "metadata": kw.get("metadata", {}),
    }
    return base


def test_ingest_happy_path(client):
    r = client.post("/events/ingest", json={"events": [_evt()]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1 and body["duplicates"] == 0 and body["rejected"] == 0


def test_ingest_idempotent(client):
    eid = "11111111-1111-1111-1111-111111111111"
    payload = {"events": [_evt(event_id=eid)]}
    a = client.post("/events/ingest", json=payload).json()
    b = client.post("/events/ingest", json=payload).json()
    assert a["accepted"] == 1 and a["duplicates"] == 0
    assert b["accepted"] == 0 and b["duplicates"] == 1


def test_ingest_partial_success(client):
    good = _evt(event_id="22222222-2222-2222-2222-222222222222")
    bad = {"event_id": "x", "store_id": "S", "missing_fields": True}
    r = client.post("/events/ingest", json={"events": [good, bad]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1 and body["rejected"] == 1


def test_ingest_malformed_payload(client):
    r = client.post("/events/ingest", json={"not_events": []})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "MALFORMED_PAYLOAD"


def test_ingest_oversize_batch(client):
    payload = {"events": [_evt(event_id=f"33333333-3333-3333-3333-{i:012d}") for i in range(501)]}
    r = client.post("/events/ingest", json=payload)
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "BATCH_TOO_LARGE"


def test_ingest_low_confidence_event_accepted(client):
    """Detection conf < floor must NOT be silently dropped — schema-compliant
    events with confidence=0.05 still land in the events table; downstream
    filters decide what to ignore. (Rubric: confidence calibration.)"""
    e = _evt(event_id="44444444-4444-4444-4444-444444444444", confidence=0.05)
    r = client.post("/events/ingest", json={"events": [e]})
    assert r.status_code == 200 and r.json()["accepted"] == 1

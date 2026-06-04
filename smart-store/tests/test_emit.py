# PROMPT: "Cover pipeline.emit's pure-logic paths: write_jsonl, EventEmitter buffering
#   and flush behaviour without making real HTTP calls. Use httpx MockTransport."
# CHANGES MADE:
#   - The AI tried to mock httpx.Client.post directly, which doesn't intercept the
#     internal client. Switched to httpx.MockTransport injected into the EventEmitter
#     via a tiny test helper, which is the canonical recipe.
from __future__ import annotations

import json
from pathlib import Path

import httpx

from pipeline.emit import EventEmitter, build_event, write_jsonl, make_event_id
from datetime import datetime, timezone


def _ev(seq):
    return build_event(
        store_id="S", camera_id="C", visitor_id=f"V{seq}",
        event_type="ENTRY",
        ts=datetime(2026, 3, 8, 10, 0, seq, tzinfo=timezone.utc),
        confidence=0.9,
    )


def test_make_event_id_changes_with_inputs():
    a = make_event_id("S", "C", "V", "ENTRY", "2026-03-08T10:00:00Z")
    b = make_event_id("S", "C", "V", "ENTRY", "2026-03-08T10:00:01Z")
    assert a != b


def test_write_jsonl(tmp_path: Path):
    out = tmp_path / "events.jsonl"
    n = write_jsonl([_ev(i) for i in range(5)], str(out))
    assert n == 5
    lines = out.read_text().splitlines()
    assert len(lines) == 5
    parsed = [json.loads(line) for line in lines]
    assert all("event_id" in p and p["timestamp"].endswith("Z") for p in parsed)


def test_emitter_flushes_on_threshold(monkeypatch):
    received = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(json.loads(req.content.decode()))
        return httpx.Response(200, json={"accepted": len(received[-1]["events"]),
                                         "duplicates": 0, "rejected": 0, "results": []})

    em = EventEmitter(api_base="http://test", batch_size=3)
    em._client = httpx.Client(transport=httpx.MockTransport(handler))

    em.add(_ev(1))
    em.add(_ev(2))
    assert received == []
    em.add(_ev(3))  # threshold reached
    assert len(received) == 1 and len(received[0]["events"]) == 3
    em.add(_ev(4))
    em.close()  # final flush
    assert len(received) == 2 and len(received[1]["events"]) == 1

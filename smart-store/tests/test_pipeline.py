# PROMPT: "Tests for the detection pipeline's pure-logic pieces: zone polygon
#   point-in-polygon correctness, deterministic event_id (uuid5) generation, ReIDIndex
#   match-and-update behaviour, and the entry-line crossing direction logic."
# CHANGES MADE:
#   - The AI's polygon test used a degenerate triangle that happened to pass on the
#     wrong implementation. Replaced with two convex polygons and explicit edge cases.
#   - The ReID test originally compared raw embeddings; I switched to verifying that the
#     match function returns the *correct identity* under a small perturbation, which is
#     what we actually rely on in production.
from __future__ import annotations

import numpy as np
from datetime import datetime, timezone

from pipeline.emit import build_event, make_event_id
from pipeline.reid import ReIDIndex
from pipeline.run import _crossed_entry_line
from pipeline.zones import find_zone, point_in_polygon, Zone


def test_point_in_polygon_basic():
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert point_in_polygon((0.5, 0.5), square) is True
    assert point_in_polygon((1.5, 0.5), square) is False
    assert point_in_polygon((-0.1, -0.1), square) is False


def test_find_zone():
    zones = [
        Zone(zone_id="A", polygon_norm=[(0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.0, 0.4)]),
        Zone(zone_id="B", polygon_norm=[(0.5, 0.5), (0.9, 0.5), (0.9, 0.9), (0.5, 0.9)]),
    ]
    assert find_zone((0.2, 0.2), zones) == "A"
    assert find_zone((0.6, 0.6), zones) == "B"
    assert find_zone((0.45, 0.45), zones) is None  # gap between A and B


def test_make_event_id_is_deterministic():
    a = make_event_id("S", "C", "V", "ENTRY", "2026-03-08T10:00:00Z")
    b = make_event_id("S", "C", "V", "ENTRY", "2026-03-08T10:00:00Z")
    assert a == b
    c = make_event_id("S", "C", "V", "EXIT", "2026-03-08T10:00:00Z")
    assert a != c


def test_build_event_schema_compliant():
    e = build_event(
        store_id="S", camera_id="CAM", visitor_id="VIS_1",
        event_type="ZONE_ENTER", ts=datetime(2026, 3, 8, tzinfo=timezone.utc),
        zone_id="LIPSTICK", confidence=0.91,
    )
    for key in ("event_id", "store_id", "camera_id", "visitor_id", "event_type",
                "timestamp", "zone_id", "dwell_ms", "is_staff", "confidence", "metadata"):
        assert key in e
    assert e["timestamp"].endswith("Z")
    assert 0.0 <= e["confidence"] <= 1.0


def test_reid_match_and_reentry():
    idx = ReIDIndex(threshold=0.8, reentry_window_sec=600)
    now = datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc)
    emb_a = np.ones(96, dtype=np.float32) / np.sqrt(96)
    ident = idx.add("VIS_a", emb_a, now)
    # exact match returns same identity
    assert idx.match(emb_a, now) is ident
    # mark exited; small perturbation should still match within window
    idx.mark_exited(ident, now)
    later = datetime(2026, 3, 8, 10, 5, 0, tzinfo=timezone.utc)
    perturbed = emb_a + np.random.RandomState(0).normal(0, 0.005, size=emb_a.shape).astype(np.float32)
    perturbed = perturbed / float(np.linalg.norm(perturbed))
    assert idx.match(perturbed, later) is ident
    assert idx.is_reentry(ident, later) is True


def test_reid_evicts_identities_past_max_lifetime():
    """A long-stayer's embedding must be evicted from the index once it
    has lived longer than `max_lifetime_sec`, so a similarly-dressed new
    arrival doesn't falsely register as a REENTRY of that earlier session.

    Without eviction, a customer who entered at t=0 and is still being
    tracked at t=4h+ keeps their embedding alive in the index forever:
    any new arrival in similar clothing would match the stale embedding
    and emit a false REENTRY event with the old visitor_id."""
    idx = ReIDIndex(threshold=0.8, reentry_window_sec=86400, max_lifetime_sec=2700)
    t0 = datetime(2026, 3, 8, 10, 0, 0, tzinfo=timezone.utc)
    emb = np.ones(96, dtype=np.float32) / np.sqrt(96)
    idx.add("VIS_long_stayer", emb, t0)
    # Within the lifetime cap → match still works.
    t_mid = datetime(2026, 3, 8, 10, 30, 0, tzinfo=timezone.utc)
    assert idx.match(emb, t_mid) is not None
    # Past the lifetime cap → identity gone, no match.
    t_late = datetime(2026, 3, 8, 11, 0, 0, tzinfo=timezone.utc)  # 1h after add
    assert idx.match(emb, t_late) is None
    assert idx.identities == [], "expired identity should have been evicted"


def test_entry_line_crossing_directions():
    line_down = {"y_threshold": 0.5, "inbound_direction": "down"}
    assert _crossed_entry_line(0.4, 0.6, line_down) == "ENTRY"
    assert _crossed_entry_line(0.6, 0.4, line_down) == "EXIT"
    assert _crossed_entry_line(0.6, 0.7, line_down) is None  # both above

    line_up = {"y_threshold": 0.5, "inbound_direction": "up"}
    assert _crossed_entry_line(0.6, 0.4, line_up) == "ENTRY"
    assert _crossed_entry_line(0.4, 0.6, line_up) == "EXIT"

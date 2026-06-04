# PROMPT: "Cover pure-logic paths in pipeline/run.py without invoking YOLO. Test
#   clip_start determinism, _adopt_floor_track cross-camera dedup window, and the
#   billing-camera state machine emitting JOIN/ABANDON correctly."
# CHANGES MADE:
#   - The first version called process_billing_camera with detections built ad-hoc;
#     it didn't match the actual Detection dataclass shape. Switched to building real
#     Detection objects so the test exercises the production path.
#   - Used a tiny in-process emitter that just collects events to assert against —
#     simpler than mocking httpx.
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from pipeline.detect import Detection
from pipeline.run import (
    PipelineState,
    _adopt_floor_track,
    _new_visitor_id,
    clip_start,
    detect_to_ts,
    process_billing_camera,
    process_entry_camera,
    process_floor_camera,
)
from pipeline.zones import StoreLayout, CameraLayout, Zone


def test_clip_start_is_deterministic(tmp_path):
    a = clip_start(Path("foo.mp4"))
    b = clip_start(Path("foo.mp4"))
    c = clip_start(Path("bar.mp4"))
    assert a == b
    assert a != c  # different filename → different offset


def test_new_visitor_id_format():
    vid = _new_visitor_id()
    assert vid.startswith("VIS_") and len(vid) == 12


def test_detect_to_ts_advances_with_frame_idx():
    base = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    a = detect_to_ts(base, 0)
    b = detect_to_ts(base, 30)
    assert (b - a).total_seconds() > 0


def test_adopt_floor_track_uses_recent_entry():
    layout = StoreLayout(
        store_id="S",
        cameras={"FLOOR": CameraLayout(name="FLOOR", role="floor", overlap_with=["ENTRY"])},
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    ts = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    state.recent_entries.append((ts, "VIS_known", 0.5, 0.5, ["FLOOR"]))
    later = datetime(2026, 3, 8, 18, 0, 3, tzinfo=timezone.utc)
    visitor_id = _adopt_floor_track(state, "FLOOR", 0.5, 0.5, later)
    assert visitor_id == "VIS_known"


def test_adopt_floor_track_skips_non_overlap_cam():
    """Entry declares overlap_with=[CAM_ZONE_1]. A track born on CAM_ZONE_2
    within the 3-second window must NOT inherit the entry's visitor_id —
    those cameras don't share a field of view."""
    layout = StoreLayout(store_id="S", cameras={}, clip_camera_map={})
    state = PipelineState(layout=layout)
    ts = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    state.recent_entries.append((ts, "VIS_known", 0.5, 0.5, ["CAM_ZONE_1"]))
    later = datetime(2026, 3, 8, 18, 0, 2, tzinfo=timezone.utc)
    assert _adopt_floor_track(state, "CAM_ZONE_2", 0.5, 0.5, later) is None
    assert _adopt_floor_track(state, "CAM_BILLING", 0.5, 0.5, later) is None
    # The declared cam still adopts.
    assert _adopt_floor_track(state, "CAM_ZONE_1", 0.5, 0.5, later) == "VIS_known"


def test_adopt_floor_track_empty_overlap_requires_spatial_proximity():
    """With no declared overlap, the spatial-proximity fallback gates adoption.
    A far-apart track (dist > 0.4) must NOT adopt; a close one must."""
    layout = StoreLayout(store_id="S", cameras={}, clip_camera_map={})
    state = PipelineState(layout=layout)
    ts = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    state.recent_entries.append((ts, "VIS_known", 0.1, 0.1, []))
    later = datetime(2026, 3, 8, 18, 0, 2, tzinfo=timezone.utc)
    # (0.9, 0.9) is ~1.13 normalised distance from (0.1, 0.1) — far past 0.4.
    assert _adopt_floor_track(state, "FLOOR", 0.9, 0.9, later) is None
    # A nearby track on the same frame is adopted.
    assert _adopt_floor_track(state, "FLOOR", 0.15, 0.15, later) == "VIS_known"


def test_billing_camera_emits_join_then_abandon():
    layout = StoreLayout(
        store_id="S",
        cameras={
            "BILL": CameraLayout(
                name="BILL",
                role="billing",
                queue_polygon=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["BILL"]

    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    ts = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Frame 1: one detection inside queue polygon → JOIN
    process_billing_camera(state, "BILL", cam,
                           {1: Detection(0.4, 0.4, 0.6, 0.6, 0.9)}, ts, sink)
    later = datetime(2026, 3, 8, 18, 0, 5, tzinfo=timezone.utc)
    # Frame 2: visitor leaves queue → ABANDON
    process_billing_camera(state, "BILL", cam, {}, later, sink)

    types = [e["event_type"] for e in captured]
    assert "BILLING_QUEUE_JOIN" in types
    assert "BILLING_QUEUE_ABANDON" in types
    join = next(e for e in captured if e["event_type"] == "BILLING_QUEUE_JOIN")
    assert join["metadata"]["queue_depth"] == 1


def test_billing_abandon_uses_rolling_min_confidence():
    """M-6: ABANDON's emitted confidence reflects the WORST observation made
    during the queue stay, not a hardcoded 0.5 marker. Spec §3.3 says
    low-conf events must be FLAGGED, not silently elevated; the prior fallback
    invented a midpoint value indistinguishable from a real 0.5 detection.
    """
    layout = StoreLayout(
        store_id="S",
        cameras={
            "BILL": CameraLayout(
                name="BILL", role="billing",
                queue_polygon=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["BILL"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    # Visitor in queue at three timestamps with confidences 0.9, 0.42, 0.71.
    # Rolling min = 0.42, which should appear on the ABANDON event.
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 3, 8, 18, 0, 2, tzinfo=timezone.utc)
    t3 = datetime(2026, 3, 8, 18, 0, 8, tzinfo=timezone.utc)  # >5s gap → ABANDON

    process_billing_camera(state, "BILL", cam,
                           {1: Detection(0.4, 0.4, 0.6, 0.6, 0.90)}, t0, sink)
    process_billing_camera(state, "BILL", cam,
                           {1: Detection(0.4, 0.4, 0.6, 0.6, 0.42)}, t1, sink)
    process_billing_camera(state, "BILL", cam,
                           {1: Detection(0.4, 0.4, 0.6, 0.6, 0.71)}, t2, sink)
    process_billing_camera(state, "BILL", cam, {}, t3, sink)

    abandon = next(e for e in captured if e["event_type"] == "BILLING_QUEUE_ABANDON")
    assert abandon["confidence"] == 0.42, (
        f"expected rolling min 0.42 (worst observation), got {abandon['confidence']}"
    )


def test_floor_camera_emits_zone_enter_and_dwell():
    """A track that stays in a zone long enough produces ZONE_ENTER + ZONE_DWELL."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "FLOOR": CameraLayout(
                name="FLOOR",
                role="floor",
                zones=[Zone(zone_id="LIPSTICK", polygon_norm=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])],
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["FLOOR"]

    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Track 7 enters at frame 0
    process_floor_camera(state, "FLOOR", cam,
                         {7: Detection(0.4, 0.4, 0.6, 0.6, 0.85)}, t0, {}, sink)
    # 31 seconds later → ZONE_DWELL emit threshold
    t1 = datetime(2026, 3, 8, 18, 0, 31, tzinfo=timezone.utc)
    process_floor_camera(state, "FLOOR", cam,
                         {7: Detection(0.4, 0.4, 0.6, 0.6, 0.85)}, t1, {}, sink)

    types = [e["event_type"] for e in captured]
    assert "ZONE_ENTER" in types
    assert "ZONE_DWELL" in types
    dwell = next(e for e in captured if e["event_type"] == "ZONE_DWELL")
    assert dwell["zone_id"] == "LIPSTICK"
    assert dwell["dwell_ms"] >= 30_000


def test_group_entry_marks_group_size():
    """3 detections crossing the entry line within 1 second → all 3 ENTRY events
    are held in the pending-group queue until the 2s window closes, then
    released atomically with metadata.group_size == 3 on every event."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "ENT": CameraLayout(
                name="ENT",
                role="entry",
                entry_line={"y_threshold": 0.5, "inbound_direction": "down", "x_range": [0.0, 1.0]},
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["ENT"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Prime last_seen_y with all three tracks above the line at the same frame.
    process_entry_camera(state, "ENT", cam, {
        10: Detection(0.20, 0.30, 0.30, 0.45, 0.90),
        11: Detection(0.50, 0.30, 0.60, 0.45, 0.90),
        12: Detection(0.80, 0.30, 0.90, 0.45, 0.90),
    }, t0, {}, sink)

    # Three sequential frames within 1 s — each track crosses the line one at a time.
    t1 = datetime(2026, 3, 8, 18, 0, 0, 200_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        10: Detection(0.20, 0.55, 0.30, 0.65, 0.92),
        11: Detection(0.50, 0.30, 0.60, 0.45, 0.92),
        12: Detection(0.80, 0.30, 0.90, 0.45, 0.92),
    }, t1, {}, sink)

    t2 = datetime(2026, 3, 8, 18, 0, 0, 500_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        10: Detection(0.20, 0.55, 0.30, 0.65, 0.92),
        11: Detection(0.50, 0.55, 0.60, 0.65, 0.92),
        12: Detection(0.80, 0.30, 0.90, 0.45, 0.92),
    }, t2, {}, sink)

    t3 = datetime(2026, 3, 8, 18, 0, 0, 800_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        10: Detection(0.20, 0.55, 0.30, 0.65, 0.92),
        11: Detection(0.50, 0.55, 0.60, 0.65, 0.92),
        12: Detection(0.80, 0.55, 0.90, 0.65, 0.92),
    }, t3, {}, sink)

    # Advance past the 2-second group window so the held batch flushes.
    t_flush = datetime(2026, 3, 8, 18, 0, 3, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {}, t_flush, {}, sink)

    entries = [e for e in captured if e["event_type"] == "ENTRY"]
    assert len(entries) == 3, f"expected 3 ENTRY events, got {len(entries)}: {entries}"
    sizes = [e["metadata"]["group_size"] for e in entries]
    assert sizes == [3, 3, 3], f"group_size should be 3 on all entries after atomic flush; got {sizes}"


def test_group_atomic_flush_survives_intermediate_emitter_flush():
    """Regression: previously, ENTRY events were emitted immediately with a
    provisional group_size and back-stamped in-place inside the emitter
    buffer. If the emitter's batch flush ran between two arrivals in the
    same 2-second window, earlier events shipped with `group_size=N` while
    later events shipped with `group_size=N+1` — a race that would never be
    reconciled. The new contract holds events in `cs.pending_group` until
    the window closes, so this test exercises a sink that flushes after
    *every* event and still asserts a consistent final group_size."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "ENT": CameraLayout(
                name="ENT",
                role="entry",
                entry_line={"y_threshold": 0.5, "inbound_direction": "down", "x_range": [0.0, 1.0]},
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["ENT"]

    flushed: list[dict] = []  # what the API would actually see, in order

    class _AggressiveSink:
        """Mirrors the worst case: every add() triggers a flush."""
        def add(self, e):
            flushed.append(e)

    sink = _AggressiveSink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Prime three tracks above the line.
    process_entry_camera(state, "ENT", cam, {
        20: Detection(0.20, 0.30, 0.30, 0.45, 0.90),
        21: Detection(0.50, 0.30, 0.60, 0.45, 0.90),
        22: Detection(0.80, 0.30, 0.90, 0.45, 0.90),
    }, t0, {}, sink)

    # Each crosses on a separate frame, ~300 ms apart.
    t1 = datetime(2026, 3, 8, 18, 0, 0, 300_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        20: Detection(0.20, 0.55, 0.30, 0.65, 0.91),
        21: Detection(0.50, 0.30, 0.60, 0.45, 0.91),
        22: Detection(0.80, 0.30, 0.90, 0.45, 0.91),
    }, t1, {}, sink)
    t2 = datetime(2026, 3, 8, 18, 0, 0, 600_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        20: Detection(0.20, 0.55, 0.30, 0.65, 0.92),
        21: Detection(0.50, 0.55, 0.60, 0.65, 0.92),
        22: Detection(0.80, 0.30, 0.90, 0.45, 0.92),
    }, t2, {}, sink)
    t3 = datetime(2026, 3, 8, 18, 0, 0, 900_000, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {
        20: Detection(0.20, 0.55, 0.30, 0.65, 0.93),
        21: Detection(0.50, 0.55, 0.60, 0.65, 0.93),
        22: Detection(0.80, 0.55, 0.90, 0.65, 0.93),
    }, t3, {}, sink)
    # Advance past the window — atomic flush.
    t_flush = datetime(2026, 3, 8, 18, 0, 3, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {}, t_flush, {}, sink)

    entries = [e for e in flushed if e["event_type"] == "ENTRY"]
    assert len(entries) == 3
    sizes = {e["metadata"]["group_size"] for e in entries}
    # The whole group must agree — no `{2, 3}` mix that the old race produced.
    assert sizes == {3}, f"every entry in a co-arrival group must share group_size; got {sizes}"


def test_solo_arrival_is_group_size_one():
    """A lone arrival outside the 2 s window has group_size == 1."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "ENT": CameraLayout(
                name="ENT",
                role="entry",
                entry_line={"y_threshold": 0.5, "inbound_direction": "down", "x_range": [0.0, 1.0]},
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["ENT"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam,
                         {3: Detection(0.4, 0.3, 0.6, 0.4, 0.9)}, t0, {}, sink)
    t1 = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam,
                         {3: Detection(0.4, 0.55, 0.6, 0.65, 0.92)}, t1, {}, sink)
    # Advance past the group window so the lone ENTRY flushes.
    t_flush = datetime(2026, 3, 8, 18, 0, 4, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam, {}, t_flush, {}, sink)

    entries = [e for e in captured if e["event_type"] == "ENTRY"]
    assert len(entries) == 1
    assert entries[0]["metadata"]["group_size"] == 1


def test_entry_camera_emits_entry_and_exit():
    layout = StoreLayout(
        store_id="S",
        cameras={
            "ENT": CameraLayout(
                name="ENT",
                role="entry",
                entry_line={"y_threshold": 0.5, "inbound_direction": "down", "x_range": [0.0, 1.0]},
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["ENT"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Frame 0: above the line (no crossing yet — last_seen primed)
    process_entry_camera(state, "ENT", cam,
                         {3: Detection(0.4, 0.3, 0.6, 0.4, 0.9)}, t0, {}, sink)
    # Frame 1: now below the line → ENTRY
    t1 = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam,
                         {3: Detection(0.4, 0.55, 0.6, 0.65, 0.92)}, t1, {}, sink)
    # Frame 2: back above the line → EXIT
    t2 = datetime(2026, 3, 8, 18, 0, 5, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam,
                         {3: Detection(0.4, 0.3, 0.6, 0.4, 0.93)}, t2, {}, sink)

    types = [e["event_type"] for e in captured]
    assert "ENTRY" in types and "EXIT" in types


def _entry_layout(y_threshold: float = 0.55, inbound: str = "down") -> StoreLayout:
    return StoreLayout(
        store_id="S",
        cameras={
            "ENT": CameraLayout(
                name="ENT",
                role="entry",
                entry_line={"y_threshold": y_threshold, "inbound_direction": inbound, "x_range": [0.0, 1.0]},
            )
        },
        clip_camera_map={},
    )


def test_entry_camera_does_not_count_passerby():
    """A track first-seen *past* the entry line is NOT a fresh ENTRY — it's most
    likely a person walking past the storefront on the outside aisle. Without
    a prior in-corridor sample we can't distinguish 'stepped through the door'
    from 'walked past on the outside', so we must abstain. This is the bug the
    user reported: Store 1's entry camera was flagging mall-aisle pedestrians
    as customers."""
    state = PipelineState(layout=_entry_layout())
    cam = state.layout.cameras["ENT"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Frame 0: track first-seen at cy=0.7 — already past the threshold
    process_entry_camera(state, "ENT", cam,
                         {7: Detection(0.4, 0.65, 0.6, 0.75, 0.9)}, t0, {}, sink)
    # Frame 1: same track, still past the threshold and walking outward
    t1 = datetime(2026, 3, 8, 18, 0, 1, tzinfo=timezone.utc)
    process_entry_camera(state, "ENT", cam,
                         {7: Detection(0.4, 0.7, 0.6, 0.8, 0.91)}, t1, {}, sink)

    types = [e["event_type"] for e in captured]
    assert "ENTRY" not in types, f"passer-by should not produce ENTRY, got {types}"


def test_floor_bootstrap_flap_does_not_emit_entry():
    """A bootstrap-synthetic ENTRY is staged on the session, not emitted, until
    the track has persisted at least BOOTSTRAP_PROMOTE_S. A 1-frame flap
    (track_id appearing then vanishing within a few hundred ms) must NOT
    produce a phantom ENTRY — that's what was inflating Store 1's
    BillingQueue counts before this gate."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "FL": CameraLayout(
                name="FL",
                role="floor",
                zones=[Zone(zone_id="Z1", polygon_norm=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])],
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["FL"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Frame 0: track 11 bootstraps (no recent entry, no ReID match yet)
    process_floor_camera(state, "FL", cam,
                         {11: Detection(0.4, 0.4, 0.5, 0.5, 0.9)}, t0, {}, sink)
    # Track disappears immediately — single-frame flap. No further frames.

    entries = [e for e in captured if e["event_type"] == "ENTRY"]
    assert entries == [], f"flap must not emit ENTRY, got {entries}"


def test_floor_bootstrap_promotes_after_persistence():
    """A bootstrap-synthetic ENTRY emits exactly once after the track has
    been seen for at least BOOTSTRAP_PROMOTE_S. The emitted event's
    timestamp matches the original bootstrap time so the timeline is honest."""
    layout = StoreLayout(
        store_id="S",
        cameras={
            "FL": CameraLayout(
                name="FL",
                role="floor",
                zones=[Zone(zone_id="Z1", polygon_norm=[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])],
            )
        },
        clip_camera_map={},
    )
    state = PipelineState(layout=layout)
    cam = layout.cameras["FL"]
    captured: list[dict] = []

    class _Sink:
        def add(self, e):
            captured.append(e)

    sink = _Sink()
    t0 = datetime(2026, 3, 8, 18, 0, 0, tzinfo=timezone.utc)
    # Bootstrap at t0, promote at t1 = t0 + 2 s (above the 1.5 s threshold)
    process_floor_camera(state, "FL", cam,
                         {11: Detection(0.4, 0.4, 0.5, 0.5, 0.9)}, t0, {}, sink)
    t1 = datetime(2026, 3, 8, 18, 0, 2, tzinfo=timezone.utc)
    process_floor_camera(state, "FL", cam,
                         {11: Detection(0.41, 0.41, 0.51, 0.51, 0.9)}, t1, {}, sink)

    entries = [e for e in captured if e["event_type"] == "ENTRY"]
    assert len(entries) == 1, f"expected exactly one promoted ENTRY, got {len(entries)}"
    # Confidence reflects the actual detection at promotion time (det.confidence),
    # not a marker. Spec: emitted confidence must reflect signal quality.
    assert entries[0]["confidence"] == 0.9
    # Timestamp must be the original bootstrap ts (t0), not t1 — the timeline
    # should reflect when the visitor actually appeared, not when we promoted.
    assert entries[0]["timestamp"].startswith("2026-03-08T18:00:00")

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
    end up tagged with metadata.group_size == 3 (back-stamping is what makes
    the earlier-emitted events match the final group count)."""
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

    entries = [e for e in captured if e["event_type"] == "ENTRY"]
    assert len(entries) == 3, f"expected 3 ENTRY events, got {len(entries)}: {entries}"
    sizes = [e["metadata"]["group_size"] for e in entries]
    assert sizes == [3, 3, 3], f"group_size should be back-stamped to 3 on all entries; got {sizes}"


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

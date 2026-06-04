"""Pipeline orchestrator: clip(s) → events.

Usage:
    python -m pipeline.run --store STORE_BLR_001 --clip-dir "data/Store 1"

For each clip in the directory whose filename matches an entry in the layout's
clip_camera_map, we run the YOLO+ByteTrack stream and feed detections through
camera-specific state machines:

    entry cam   → ENTRY / EXIT / REENTRY (with re-ID)
    floor cam   → ZONE_ENTER / ZONE_EXIT / ZONE_DWELL
    billing cam → BILLING_QUEUE_JOIN / BILLING_QUEUE_ABANDON + queue_depth

Cross-camera identity:
    Tracks born on a floor cam within 3 s of an ENTRY event in the spatial
    overlap region inherit that visitor_id. We persist the entry events
    in-memory and consult them when a new floor track appears.

Output:
    Events are POSTed in batches of 200 to the API. With --no-emit they're
    written to events.jsonl in the cwd instead — useful for offline grading.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import CONFIG
from .detect import Detection, YoloPersonDetector
from .emit import EventEmitter, build_event, write_jsonl
from .reid import ReIDIndex, TrackIdentity
from .session import VisitorSession, ZoneDwell
from .staff import StaffClassifier
from .zones import StoreLayout, find_zone, load_layout, point_in_polygon

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("pipeline.run")


@dataclass
class CameraState:
    """Per-camera scratch state."""
    track_to_visitor: dict[int, str] = field(default_factory=dict)  # local track_id -> visitor_id
    last_seen_y: dict[int, float] = field(default_factory=dict)
    # First y observed for each track id. Used by the warm-up branch of
    # _crossed_entry_line: a track that first appears on the inside of the
    # entry line (within the entry-camera x_range) is attributed to a crossing
    # that happened in the frames we skipped between samples.
    warmed_up: set[int] = field(default_factory=set)
    track_lost_at: dict[int, datetime] = field(default_factory=dict)
    # ENTRY events emitted within the last 2 s, kept as references so we can
    # back-stamp metadata.group_size when more arrivals land in the same
    # window. Tuples: (ts, event_dict).
    recent_entries_for_grouping: list[tuple[datetime, dict]] = field(default_factory=list)


@dataclass
class PipelineState:
    layout: StoreLayout
    sessions: dict[str, VisitorSession] = field(default_factory=dict)
    reid: ReIDIndex = field(default_factory=ReIDIndex)
    staff: StaffClassifier = field(default_factory=StaffClassifier)
    cam_state: dict[str, CameraState] = field(default_factory=lambda: defaultdict(CameraState))
    recent_entries: deque = field(default_factory=lambda: deque(maxlen=64))


def clip_start(clip_path: Path) -> datetime:
    """Best-effort timestamp for the clip start. The PDF says the timestamp is
    derived from clip_start_utc + frame_idx/fps, but we don't have a manifest
    file.

    We hash the filename into a deterministic minute-offset from the configured
    PIPELINE_CLIP_START anchored at *midnight UTC of the base day*, capped at
    4 hours of spread. Anchoring at midnight + a 0..4h spread guarantees every
    clip from a single run lands on the same calendar day — important because
    the dashboard's "today window" pins to that day, and an offset that wraps
    across midnight would silently split metrics across two days.

    Determinism: `name_hash` is the digest of the filename (not Python's salted
    `hash()`), so reruns of the same input produce the same timestamps and
    idempotency holds across invocations.
    """
    import hashlib

    base = datetime.fromisoformat(CONFIG.default_clip_start_iso.replace("Z", "+00:00"))
    midnight = base.replace(hour=0, minute=0, second=0, microsecond=0)
    digest = hashlib.sha256(clip_path.name.encode("utf-8")).digest()
    name_hash = int.from_bytes(digest[:8], "big")
    h = name_hash % (60 * 4)  # minute offset, 0..4h — same day guaranteed
    return midnight + timedelta(minutes=h)


def detect_to_ts(start: datetime, frame_idx: int) -> datetime:
    return start + timedelta(seconds=frame_idx / CONFIG.fps)


def _new_visitor_id() -> str:
    return f"VIS_{uuid.uuid4().hex[:8]}"


def _crossed_entry_line(
    prev_y: Optional[float],
    cur_y: float,
    line: dict,
    *,
    first_seen: bool = False,
) -> str | None:
    """Return 'ENTRY' or 'EXIT' if the line was crossed this frame.

    If `first_seen` is true the track had no previous observation in the entry
    corridor — we attribute the missing crossing to a skipped frame and treat
    a current-position-on-the-inside as a fresh ENTRY. (We never synthesise
    EXITs from the warm-up branch: a track that vanishes after exiting was
    already gated on a real prev_y < threshold sample.)"""
    if line is None:
        return None
    yt = float(line.get("y_threshold", 0.5))
    inbound = line.get("inbound_direction", "down")
    if prev_y is None:
        if not first_seen:
            return None
        # Warm-up: first observation past the line is treated as ENTRY.
        if inbound == "down" and cur_y >= yt:
            return "ENTRY"
        if inbound == "up" and cur_y <= yt:
            return "ENTRY"
        return None
    if inbound == "down":
        if prev_y < yt <= cur_y:
            return "ENTRY"
        if prev_y > yt >= cur_y:
            return "EXIT"
    else:  # inbound == "up"
        if prev_y > yt >= cur_y:
            return "ENTRY"
        if prev_y < yt <= cur_y:
            return "EXIT"
    return None


def process_entry_camera(
    state: PipelineState,
    cam_id: str,
    cam,
    detections: dict[int, Detection],
    ts: datetime,
    crops: dict[int, "any"],
    emitter: "EventEmitter",
) -> None:
    cs = state.cam_state[cam_id]
    line = cam.entry_line or {}
    x_lo, x_hi = (line.get("x_range") or [0.0, 1.0])

    # Drop track ids that disappeared this frame (tracking handed off)
    seen_now = set(detections)
    for tid in list(cs.last_seen_y):
        if tid not in seen_now:
            cs.track_lost_at[tid] = ts

    for tid, det in detections.items():
        cy = det.cy_norm
        cx = det.cx_norm
        if not (x_lo <= cx <= x_hi):
            cs.last_seen_y[tid] = cy
            continue
        prev_y = cs.last_seen_y.get(tid)
        # First time we see this track inside the entry corridor: attribute
        # any missing crossing to a frame we skipped (frame_stride > 1 can
        # easily skip a sub-second crossing). After the first sample we have
        # a real prev_y and the standard threshold-cross logic takes over.
        first_seen = tid not in cs.warmed_up
        crossing = _crossed_entry_line(prev_y, cy, line, first_seen=first_seen)
        cs.last_seen_y[tid] = cy
        cs.warmed_up.add(tid)
        if not crossing:
            continue

        # Re-identify on threshold crossing only — costs are bounded
        crop = crops.get(tid)
        emb = state.reid.compute_embedding(crop)
        existing = state.reid.match(emb, ts)

        if crossing == "ENTRY":
            if existing and state.reid.is_reentry(existing, ts):
                state.reid.update(existing, emb, ts)
                visitor_id = existing.visitor_id
                event_type = "REENTRY"
                sess = state.sessions.get(visitor_id) or VisitorSession(visitor_id, ts)
                sess.entered_at = ts
                sess.exited_at = None
                state.sessions[visitor_id] = sess
            else:
                visitor_id = _new_visitor_id()
                state.reid.add(visitor_id, emb, ts)
                state.sessions[visitor_id] = VisitorSession(visitor_id=visitor_id, entered_at=ts)
                event_type = "ENTRY"
            cs.track_to_visitor[tid] = visitor_id
            sess = state.sessions[visitor_id]
            sess.is_staff = state.staff.classify(visitor_id, crop_bgr=crop)
            seq = sess.next_seq()

            # ----- group_size: back-stamp prior ENTRY/REENTRY events ---------
            # Drop entries older than 2 s, then count what remains plus this
            # one. The group_size we stamp on this event is "how many people
            # had arrived within the 2-second window at the moment I emitted".
            # We also back-stamp the in-window prior events so they all share
            # the final count — works as long as none of them have flushed
            # yet (typical batch_size is 200, group windows are <5 events).
            window = [
                (et, ev) for et, ev in cs.recent_entries_for_grouping
                if (ts - et).total_seconds() <= 2.0
            ]
            group_size = len(window) + 1
            for _, prior_ev in window:
                prior_ev["metadata"]["group_size"] = group_size
            cs.recent_entries_for_grouping = window

            event = build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=visitor_id,
                event_type=event_type,
                ts=ts,
                is_staff=sess.is_staff,
                confidence=det.confidence,
                session_seq=seq,
                group_size=group_size,
            )
            cs.recent_entries_for_grouping.append((ts, event))
            emitter.add(event)
            state.recent_entries.append((ts, visitor_id, cx, cy, cam.overlap_with))
        else:  # EXIT
            visitor_id = cs.track_to_visitor.pop(tid, None)
            if existing and not visitor_id:
                visitor_id = existing.visitor_id
            if not visitor_id:
                continue
            sess = state.sessions.get(visitor_id)
            if sess is None:
                continue
            # Close any open zone dwells first
            for zone_id, zd in list(sess.zone_dwells.items()):
                dwell_ms = int((ts - zd.enter_ts).total_seconds() * 1000)
                emitter.add(build_event(
                    store_id=state.layout.store_id,
                    camera_id=cam_id,
                    visitor_id=visitor_id,
                    event_type="ZONE_EXIT",
                    ts=ts,
                    zone_id=zone_id,
                    dwell_ms=dwell_ms,
                    is_staff=sess.is_staff,
                    confidence=det.confidence,
                    session_seq=sess.next_seq(),
                ))
                sess.zone_dwells.pop(zone_id, None)
            sess.exited_at = ts
            state.reid.mark_exited(existing or state.reid.add(visitor_id, emb, ts), ts)
            emitter.add(build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=visitor_id,
                event_type="EXIT",
                ts=ts,
                is_staff=sess.is_staff,
                confidence=det.confidence,
                session_seq=sess.next_seq(),
            ))


def _adopt_floor_track(state: PipelineState, cam_id: str, cx: float, cy: float, ts: datetime) -> Optional[str]:
    """When a new track appears on a floor cam, see if a recent ENTRY event from
    an overlapping camera should claim it (cross-camera dedup)."""
    for entry_ts, visitor_id, ex, ey, overlap_cams in list(state.recent_entries):
        if (ts - entry_ts).total_seconds() > 3.0:
            continue
        if overlap_cams:
            # Layout declares which cams overlap with this entry — adopt only
            # when this cam is one of them; otherwise the entry is for a
            # different part of the store and must not claim this track.
            if cam_id in overlap_cams:
                return visitor_id
            continue
        # No declared overlap — accept any temporally-close entry only if
        # spatial proximity is plausible (within 0.4 normalised distance).
        dx = abs(cx - ex)
        dy = abs(cy - ey)
        if (dx * dx + dy * dy) ** 0.5 < 0.4:
            return visitor_id
    return None


def process_floor_camera(
    state: PipelineState,
    cam_id: str,
    cam,
    detections: dict[int, Detection],
    ts: datetime,
    crops: dict[int, "any"],
    emitter: "EventEmitter",
) -> None:
    cs = state.cam_state[cam_id]
    seen = set(detections)
    # Visitors who left the camera frame: close zone dwells if any
    for tid in list(cs.track_to_visitor):
        if tid not in seen:
            visitor_id = cs.track_to_visitor[tid]
            sess = state.sessions.get(visitor_id)
            if sess and sess.current_zone:
                zone_id = sess.current_zone
                zd = sess.zone_dwells.pop(zone_id, None)
                if zd:
                    dwell_ms = int((ts - zd.enter_ts).total_seconds() * 1000)
                    emitter.add(build_event(
                        store_id=state.layout.store_id,
                        camera_id=cam_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_EXIT",
                        ts=ts,
                        zone_id=zone_id,
                        dwell_ms=dwell_ms,
                        is_staff=sess.is_staff,
                        confidence=0.6,
                        session_seq=sess.next_seq(),
                    ))
                sess.current_zone = None

    for tid, det in detections.items():
        cx, cy = det.cx_norm, det.cy_norm
        visitor_id = cs.track_to_visitor.get(tid)
        bootstrapped = False
        if visitor_id is None:
            visitor_id = _adopt_floor_track(state, cam_id, cx, cy, ts)
            if visitor_id is None:
                # Floor track with no associated entry — treat as "in-store" visitor
                # bootstrapped on this camera (e.g. clip starts mid-store, or
                # the entry-camera missed the crossing entirely). We must emit
                # an ENTRY for them below; without it the funnel cascade
                # (`zone_visited &= entered`) silently drops every legitimate
                # zone-visiting visitor that came in this way.
                emb = state.reid.compute_embedding(crops.get(tid))
                existing = state.reid.match(emb, ts)
                if existing:
                    visitor_id = existing.visitor_id
                    state.reid.update(existing, emb, ts)
                else:
                    visitor_id = _new_visitor_id()
                    state.reid.add(visitor_id, emb, ts)
                    state.sessions[visitor_id] = VisitorSession(visitor_id=visitor_id, entered_at=ts)
                    bootstrapped = True
            cs.track_to_visitor[tid] = visitor_id

        sess = state.sessions.get(visitor_id)
        if sess is None:
            sess = VisitorSession(visitor_id=visitor_id, entered_at=ts)
            state.sessions[visitor_id] = sess

        # Staff classification on the floor camera. The classifier's cache is
        # upgrade-only, so we re-evaluate on every frame until a True is
        # found — a back-facing first-frame doesn't permanently mis-classify.
        if not sess.is_staff:
            sess.is_staff = state.staff.classify(visitor_id, crop_bgr=crops.get(tid))

        if bootstrapped:
            # Emit a synthetic ENTRY so the funnel sees this visitor. Confidence
            # marked low to distinguish from real entry-line crossings; downstream
            # consumers can filter on it if desired.
            emitter.add(build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=visitor_id,
                event_type="ENTRY",
                ts=ts,
                is_staff=sess.is_staff,
                confidence=0.4,
                session_seq=sess.next_seq(),
            ))

        zone_id = find_zone((cx, cy), cam.zones)

        # Zone transitions
        if zone_id != sess.current_zone:
            if sess.current_zone is not None:
                zd = sess.zone_dwells.pop(sess.current_zone, None)
                if zd:
                    dwell_ms = int((ts - zd.enter_ts).total_seconds() * 1000)
                    emitter.add(build_event(
                        store_id=state.layout.store_id,
                        camera_id=cam_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_EXIT",
                        ts=ts,
                        zone_id=sess.current_zone,
                        dwell_ms=dwell_ms,
                        is_staff=sess.is_staff,
                        confidence=det.confidence,
                        session_seq=sess.next_seq(),
                    ))
            if zone_id is not None:
                sess.zone_dwells[zone_id] = ZoneDwell(enter_ts=ts, last_emit_ts=ts)
                emitter.add(build_event(
                    store_id=state.layout.store_id,
                    camera_id=cam_id,
                    visitor_id=visitor_id,
                    event_type="ZONE_ENTER",
                    ts=ts,
                    zone_id=zone_id,
                    is_staff=sess.is_staff,
                    confidence=det.confidence,
                    sku_zone=zone_id,
                    session_seq=sess.next_seq(),
                ))
            sess.current_zone = zone_id

        # Periodic ZONE_DWELL emission
        if sess.current_zone is not None:
            zd = sess.zone_dwells.get(sess.current_zone)
            if zd and (ts - zd.last_emit_ts).total_seconds() >= CONFIG.dwell_emit_sec:
                dwell_ms = int((ts - zd.enter_ts).total_seconds() * 1000)
                emitter.add(build_event(
                    store_id=state.layout.store_id,
                    camera_id=cam_id,
                    visitor_id=visitor_id,
                    event_type="ZONE_DWELL",
                    ts=ts,
                    zone_id=sess.current_zone,
                    dwell_ms=dwell_ms,
                    is_staff=sess.is_staff,
                    confidence=det.confidence,
                    sku_zone=sess.current_zone,
                    session_seq=sess.next_seq(),
                ))
                zd.last_emit_ts = ts

        sess.floor_dwell_seconds += 1.0 / CONFIG.fps


def process_billing_camera(
    state: PipelineState,
    cam_id: str,
    cam,
    detections: dict[int, Detection],
    ts: datetime,
    emitter: "EventEmitter",
    crops: dict[int, "any"] | None = None,
) -> None:
    cs = state.cam_state[cam_id]
    qpoly = cam.queue_polygon or [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    in_queue: dict[int, str] = {}
    for tid, det in detections.items():
        if not point_in_polygon((det.cx_norm, det.cy_norm), qpoly):
            continue
        visitor_id = cs.track_to_visitor.get(tid)
        bootstrapped = False
        if visitor_id is None:
            adopted = _adopt_floor_track(state, cam_id, det.cx_norm, det.cy_norm, ts)
            if adopted is None:
                visitor_id = _new_visitor_id()
                bootstrapped = True
            else:
                visitor_id = adopted
            cs.track_to_visitor[tid] = visitor_id
            if visitor_id not in state.sessions:
                state.sessions[visitor_id] = VisitorSession(visitor_id=visitor_id, entered_at=ts)
        # Upgrade-only staff classification.
        sess = state.sessions[visitor_id]
        if not sess.is_staff and crops is not None:
            sess.is_staff = state.staff.classify(visitor_id, crop_bgr=crops.get(tid))
        if bootstrapped:
            # Synthetic ENTRY so this billing-zone visitor isn't filtered out
            # of the funnel cascade (`billing_joined &= entered`). The billing
            # camera missed having an entry-cam predecessor — most likely the
            # entry-line crossing was outside the 3s overlap window or the
            # entry cam didn't see them at all.
            emitter.add(build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=visitor_id,
                event_type="ENTRY",
                ts=ts,
                is_staff=sess.is_staff,
                confidence=0.4,
                session_seq=sess.next_seq(),
            ))
        in_queue[tid] = visitor_id

    queue_depth = len(in_queue)

    # JOIN events for newly-arrived visitors
    prior = set(getattr(cs, "_billing_prev", set()))
    current = set(in_queue.values())
    joined = current - prior
    abandoned = prior - current
    for visitor_id in joined:
        sess = state.sessions[visitor_id]
        sess.visited_billing = True
        emitter.add(build_event(
            store_id=state.layout.store_id,
            camera_id=cam_id,
            visitor_id=visitor_id,
            event_type="BILLING_QUEUE_JOIN",
            ts=ts,
            zone_id="BILLING",
            is_staff=sess.is_staff,
            confidence=0.85,
            queue_depth=queue_depth,
            session_seq=sess.next_seq(),
        ))
    for visitor_id in abandoned:
        sess = state.sessions.get(visitor_id)
        if not sess:
            continue
        # Treat as ABANDON if exit happened but POS correlation will resolve
        emitter.add(build_event(
            store_id=state.layout.store_id,
            camera_id=cam_id,
            visitor_id=visitor_id,
            event_type="BILLING_QUEUE_ABANDON",
            ts=ts,
            zone_id="BILLING",
            is_staff=sess.is_staff,
            confidence=0.7,
            queue_depth=queue_depth,
            session_seq=sess.next_seq(),
        ))
    cs._billing_prev = current  # type: ignore[attr-defined]


def process_clip(
    state: PipelineState,
    clip_path: "Path | str",
    cam_id: str,
    emitter: "EventEmitter",
    *,
    max_frames: int | None = None,
    frame_stride: int = 1,
    progress_every: int = 200,
) -> int:
    cam = state.layout.cameras.get(cam_id)
    if cam is None:
        log.warning("clip.no_camera_layout cam=%s", cam_id)
        return 0
    detector = YoloPersonDetector()
    # Live RTSP URLs come in as plain strings; recorded clips as Path objects.
    # YOLO.track() accepts both. The only difference for the clock is that
    # live streams start "now" (no manifest), recorded clips use the
    # deterministic per-filename hash so reruns produce stable timestamps.
    is_live = isinstance(clip_path, str) and clip_path.startswith(("rtsp://", "rtmp://", "http://", "https://"))
    if is_live:
        start_ts = datetime.now(timezone.utc)
        source_str = str(clip_path)
        source_name = source_str
    else:
        cp = Path(clip_path) if not isinstance(clip_path, Path) else clip_path
        start_ts = clip_start(cp)
        source_str = str(cp)
        source_name = cp.name
    # Entry cameras always run at stride=1: a line crossing can complete in
    # <0.5s and a missed sample is a counted-customer error. Floor and billing
    # cameras use the configured frame_stride.
    effective_stride = CONFIG.entry_stride if cam.role == "entry" else frame_stride
    log.info(
        "clip.start name=%s cam=%s role=%s start=%s stride=%d live=%s",
        source_name, cam_id, cam.role, start_ts.isoformat(), effective_stride, is_live,
    )
    n_frames = 0
    n_processed = 0

    for frame_idx, dets, frame_bgr in detector.track(source_str):
        n_frames += 1
        if effective_stride > 1 and (frame_idx % effective_stride) != 0:
            continue
        ts = detect_to_ts(start_ts, frame_idx)

        # Slice each track's bbox out of the original frame so the staff
        # classifier can read shirt/trouser colour. Detections are
        # normalised [0,1]; convert back to pixel coords for the slice and
        # skip degenerate crops (h or w < 8 px).
        crops: dict[int, "any"] = {}
        if frame_bgr is not None:
            try:
                fh, fw = frame_bgr.shape[:2]
                for tid, det in dets.items():
                    x1 = max(0, int(det.x1 * fw))
                    y1 = max(0, int(det.y1 * fh))
                    x2 = min(fw, int(det.x2 * fw))
                    y2 = min(fh, int(det.y2 * fh))
                    if (x2 - x1) >= 8 and (y2 - y1) >= 8:
                        crops[tid] = frame_bgr[y1:y2, x1:x2, :]
            except Exception as e:  # noqa: BLE001
                log.debug("crop_extract_err frame=%d err=%s", frame_idx, e)

        if cam.role == "entry":
            process_entry_camera(state, cam_id, cam, dets, ts, crops, emitter)
        elif cam.role == "billing":
            process_billing_camera(state, cam_id, cam, dets, ts, emitter, crops)
        else:
            process_floor_camera(state, cam_id, cam, dets, ts, crops, emitter)
        n_processed += 1

        if n_processed % progress_every == 0:
            # Flush pending events so the API and dashboard see progress live.
            try:
                emitter.flush()
            except Exception:
                pass
            log.info("clip.progress name=%s frame=%d processed=%d", source_name, frame_idx, n_processed)

        if max_frames is not None and n_processed >= max_frames:
            log.info("clip.max_frames_reached name=%s processed=%d", source_name, n_processed)
            break

    try:
        emitter.flush()
    except Exception:
        pass
    log.info("clip.done name=%s frames=%d processed=%d", source_name, n_frames, n_processed)
    return n_processed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True, help="store_id, e.g. STORE_BLR_001")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--clip-dir", help="Directory containing the clips for this store (batch mode)")
    src.add_argument(
        "--rtsp-url",
        help=(
            "RTSP URL of a live camera feed (live mode). The same pipeline that "
            "consumes recorded clips is used here — Ultralytics .track() accepts "
            "RTSP URLs natively. Pair with --rtsp-camera-id to map this stream "
            "to a camera in the store layout."
        ),
    )
    p.add_argument(
        "--rtsp-camera-id",
        default=None,
        help="Camera id (in the store layout) the --rtsp-url stream represents.",
    )
    p.add_argument("--layout", default=None, help="Path to store layout JSON (default: store_layouts/<store>.json)")
    p.add_argument("--no-emit", action="store_true", help="Write events.jsonl locally instead of POSTing")
    p.add_argument("--api", default=None, help="API base URL (overrides PIPELINE_API_BASE)")
    p.add_argument("--frame-stride", type=int, default=CONFIG.frame_stride, help="Process every Nth frame for floor/billing cams (entry cams always stride=1)")
    p.add_argument("--max-frames-per-clip", type=int, default=None, help="Cap processed frames per clip — useful for live demos")
    p.add_argument(
        "--vlm-dry-run",
        action="store_true",
        help=(
            "Audit the staff-classifier VLM prompt to vlm_audit.jsonl on the "
            "first 3 ambiguous crops without calling a real provider. Useful "
            "for proving Part D wiring without needing an API key."
        ),
    )
    args = p.parse_args(argv)

    layout_path = args.layout or f"store_layouts/{args.store}.json"
    if not Path(layout_path).exists():
        # try sibling app/store_layouts inside container
        alt = Path(__file__).resolve().parent.parent / "store_layouts" / f"{args.store}.json"
        if alt.exists():
            layout_path = str(alt)
    layout = load_layout(layout_path)
    state = PipelineState(layout=layout)
    # Activate per-store uniform palette for staff detection.
    state.staff.set_store(layout.store_id)
    if args.vlm_dry_run:
        state.staff.vlm_dry_run = True
        log.info("run.vlm_dry_run_enabled audit_path=%s cap=%d",
                 state.staff.vlm_audit_path, state.staff._vlm_audit_cap)

    if args.no_emit:
        events_buffer: list[dict] = []

        class _Sink:
            def add(self, e):
                events_buffer.append(e)

            def flush(self):
                pass

            def close(self):
                pass

        emitter = _Sink()  # type: ignore
    else:
        emitter = EventEmitter(api_base=args.api or CONFIG.api_base)

    matched = 0
    if args.rtsp_url:
        cam_id = args.rtsp_camera_id
        if cam_id is None:
            # Fall back to the first entry-role camera in the layout. Most live
            # demos point a single stream at the doorway, so this is the
            # right default for a single-stream test.
            for cid, c in layout.cameras.items():
                if c.role == "entry":
                    cam_id = cid
                    break
        if cam_id is None or cam_id not in layout.cameras:
            log.error(
                "run.rtsp_no_camera_match url=%s rtsp_camera_id=%s — supply --rtsp-camera-id",
                args.rtsp_url, args.rtsp_camera_id,
            )
            return 2
        log.info("run.rtsp_start url=%s cam=%s", args.rtsp_url, cam_id)
        process_clip(
            state, args.rtsp_url, cam_id, emitter,
            max_frames=args.max_frames_per_clip,
            frame_stride=args.frame_stride,
        )
        matched = 1
    else:
        clip_dir = Path(args.clip_dir)
        if not clip_dir.exists():
            log.error("run.clip_dir_missing path=%s", clip_dir)
            return 2
        files = sorted(clip_dir.iterdir())
        for f in files:
            if f.suffix.lower() not in (".mp4", ".mov", ".avi"):
                continue
            cam_id = layout.clip_camera_map.get(f.name)
            if cam_id is None:
                log.info("run.skip_unmapped name=%s", f.name)
                continue
            process_clip(
                state, f, cam_id, emitter,
                max_frames=args.max_frames_per_clip,
                frame_stride=args.frame_stride,
            )
            matched += 1

    if args.no_emit:
        out = Path("events.jsonl")
        n = write_jsonl(events_buffer, str(out))  # type: ignore
        log.info("run.no_emit_written path=%s events=%d", out, n)
    else:
        emitter.close()  # type: ignore

    log.info("run.done store=%s clips=%d", args.store, matched)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    track_lost_at: dict[int, datetime] = field(default_factory=dict)
    # ENTRY events held back from emission until the 2-second group window
    # closes. Tuples: (ts, event_dict). Once GROUP_WINDOW_S passes since the
    # last arrival, the whole batch flushes atomically with the final
    # group_size — this avoids a race where the emitter's batch flush could
    # ship some events with `group_size=N` and others with `group_size=N+1`.
    pending_group: list[tuple[datetime, dict]] = field(default_factory=list)


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
    PIPELINE_CLIP_START's hour-of-day on the base date, capped at 4 hours of
    spread. Anchoring at the base time + a 0..4h spread guarantees every clip
    from a single run lands on the same calendar day (the dashboard's "today
    window" pins to that day) AND overlaps the POS CSV's afternoon hours —
    without that overlap the 5-minute purchase-correlation window never
    matches.

    Determinism: `name_hash` is the digest of the filename (not Python's salted
    `hash()`), so reruns of the same input produce the same timestamps and
    idempotency holds across invocations.
    """
    import hashlib

    base = datetime.fromisoformat(CONFIG.default_clip_start_iso.replace("Z", "+00:00"))
    digest = hashlib.sha256(clip_path.name.encode("utf-8")).digest()
    name_hash = int.from_bytes(digest[:8], "big")
    h = name_hash % (60 * 4)  # minute offset, 0..4h — same day guaranteed
    return base + timedelta(minutes=h)


def detect_to_ts(start: datetime, frame_idx: int) -> datetime:
    return start + timedelta(seconds=frame_idx / CONFIG.fps)


def _new_visitor_id() -> str:
    return f"VIS_{uuid.uuid4().hex[:8]}"


# How long a bootstrap-synthetic visitor must continue to be tracked before
# we promote them to an emitted ENTRY event. Anything shorter is most likely
# a ByteTrack flap or an edge-of-frame partial detection — emitting an ENTRY
# for those inflated visitor counts (Store 1 had 14 phantom synthetics from
# till-area flaps before this gate was added).
BOOTSTRAP_PROMOTE_S = 1.5

# Co-arrival window for group_size annotation. ENTRY events that land
# within this many seconds of each other share a group_size (count of
# arrivals in the window). The window is held atomically: events are
# released to the emitter only after `GROUP_WINDOW_S` has passed since the
# last arrival, so the emitted group_size is final at flush time.
GROUP_WINDOW_S = 2.0


def _flush_expired_group(cs: "CameraState", now: datetime, emitter: "EventEmitter") -> None:
    """Release held ENTRY/REENTRY events whose group window has closed.

    The group window closes when the most-recent pending event is older than
    GROUP_WINDOW_S — i.e. no further arrivals could still join this group.
    On flush we re-stamp every event in the group with the final
    group_size (the window's full count) and hand the batch to the emitter
    in arrival order.
    """
    if not cs.pending_group:
        return
    last_ts = cs.pending_group[-1][0]
    if (now - last_ts).total_seconds() < GROUP_WINDOW_S:
        return
    final_size = len(cs.pending_group)
    for _, ev in cs.pending_group:
        ev["metadata"]["group_size"] = final_size
        emitter.add(ev)
    cs.pending_group.clear()


def _flush_all_pending_groups(state: "PipelineState", emitter: "EventEmitter") -> None:
    """End-of-clip safety net: flush every camera's held group regardless of
    window. Called once when a clip finishes."""
    for cs in state.cam_state.values():
        if not cs.pending_group:
            continue
        final_size = len(cs.pending_group)
        for _, ev in cs.pending_group:
            ev["metadata"]["group_size"] = final_size
            emitter.add(ev)
        cs.pending_group.clear()


def _maybe_promote_pending_entry(
    state: "PipelineState",
    sess: "VisitorSession",
    cam_id: str,
    ts: datetime,
    confidence: float,
    emitter: "EventEmitter",
) -> None:
    """If `sess` has a pending bootstrap-synthetic ENTRY and the track has
    persisted at least `BOOTSTRAP_PROMOTE_S` seconds, emit it now using the
    original bootstrap timestamp so the event timeline stays honest. Clears
    the pending state on emit. No-op if there's no pending entry or the
    persistence threshold hasn't been reached yet."""
    pending = sess.pending_entry_ts
    if pending is None:
        return
    if (ts - pending).total_seconds() < BOOTSTRAP_PROMOTE_S:
        return
    emitter.add(build_event(
        store_id=state.layout.store_id,
        camera_id=cam_id,
        visitor_id=sess.visitor_id,
        event_type="ENTRY",
        ts=pending,
        is_staff=sess.is_staff,
        confidence=confidence,
        session_seq=sess.next_seq(),
    ))
    sess.pending_entry_ts = None


def _crossed_entry_line(
    prev_y: Optional[float],
    cur_y: float,
    line: dict,
) -> str | None:
    """Return 'ENTRY' or 'EXIT' if the line was crossed this frame.

    A track must have been observed in the corridor for at least one prior
    frame before it can produce a crossing. Tracks that first appear *past*
    the threshold do NOT count as ENTRY — without a prior position we can't
    distinguish "stepped through the door" from "walked past the storefront
    on the outside aisle". Entry cameras run at stride=1, so every real
    crossing has at least two samples to inspect.
    """
    if line is None or prev_y is None:
        return None
    yt = float(line.get("y_threshold", 0.5))
    inbound = line.get("inbound_direction", "down")
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
    # Release any held group whose window has now closed.
    _flush_expired_group(cs, ts, emitter)
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
        crossing = _crossed_entry_line(prev_y, cy, line)
        cs.last_seen_y[tid] = cy
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

            # ----- group_size: hold this ENTRY in the pending group ----------
            # If the most-recent pending event is within the 2-second window
            # this arrival joins it (group grows). Otherwise this is the
            # start of a new group — flush the previous one atomically and
            # begin fresh. Holding events until the window closes means
            # group_size is final at emit time, no back-stamping race.
            if cs.pending_group:
                last_pending_ts = cs.pending_group[-1][0]
                if (ts - last_pending_ts).total_seconds() > GROUP_WINDOW_S:
                    # Previous group has closed — release it before opening
                    # the new one.
                    final_size = len(cs.pending_group)
                    for _, prior_ev in cs.pending_group:
                        prior_ev["metadata"]["group_size"] = final_size
                        emitter.add(prior_ev)
                    cs.pending_group.clear()

            event = build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=visitor_id,
                event_type=event_type,
                ts=ts,
                is_staff=sess.is_staff,
                confidence=det.confidence,
                session_seq=seq,
                # Provisional; real value stamped at flush time.
                group_size=0,
            )
            cs.pending_group.append((ts, event))
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
            # Close any open zone dwells first. Each ZONE_EXIT carries the
            # rolling min confidence of its dwell, not the entry-camera
            # detection's confidence — those are different signals.
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
                    confidence=zd.min_confidence,
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
                    # Track is gone — no current detection to reference. Use
                    # the rolling min observed during the dwell so the
                    # emitted confidence still reflects the worst-quality
                    # frame seen, rather than a hardcoded marker.
                    emitter.add(build_event(
                        store_id=state.layout.store_id,
                        camera_id=cam_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_EXIT",
                        ts=ts,
                        zone_id=zone_id,
                        dwell_ms=dwell_ms,
                        is_staff=sess.is_staff,
                        confidence=zd.min_confidence,
                        session_seq=sess.next_seq(),
                    ))
                sess.current_zone = None

    for tid, det in detections.items():
        cx, cy = det.cx_norm, det.cy_norm
        visitor_id = cs.track_to_visitor.get(tid)
        if visitor_id is None:
            visitor_id = _adopt_floor_track(state, cam_id, cx, cy, ts)
            if visitor_id is None:
                # Floor track with no associated entry — treat as "in-store"
                # visitor bootstrapped on this camera (clip started mid-store,
                # or the entry-camera missed the crossing). Stage a pending
                # synthetic ENTRY but don't emit it yet — a track that
                # disappears within BOOTSTRAP_PROMOTE_S is a ByteTrack flap.
                emb = state.reid.compute_embedding(crops.get(tid))
                existing = state.reid.match(emb, ts)
                if existing:
                    visitor_id = existing.visitor_id
                    state.reid.update(existing, emb, ts)
                else:
                    visitor_id = _new_visitor_id()
                    state.reid.add(visitor_id, emb, ts)
                    state.sessions[visitor_id] = VisitorSession(
                        visitor_id=visitor_id,
                        entered_at=ts,
                        pending_entry_ts=ts,
                    )
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

        # Promote a pending bootstrap-synthetic ENTRY once the track has
        # persisted long enough — silently drops sub-1.5s flaps. The promoted
        # ENTRY uses the actual detection confidence at promotion time so the
        # event reflects real signal quality (spec: "your detection
        # confidence — do not suppress low-conf events").
        _maybe_promote_pending_entry(state, sess, cam_id, ts, det.confidence, emitter)

        zone_id = find_zone((cx, cy), cam.zones)

        # Zone transitions
        if zone_id != sess.current_zone:
            if sess.current_zone is not None:
                zd = sess.zone_dwells.pop(sess.current_zone, None)
                if zd:
                    dwell_ms = int((ts - zd.enter_ts).total_seconds() * 1000)
                    # ZONE_EXIT confidence reflects the *exited* zone's worst
                    # frame, not the new zone's first frame. The current
                    # det.confidence belongs to the new zone.
                    emitter.add(build_event(
                        store_id=state.layout.store_id,
                        camera_id=cam_id,
                        visitor_id=visitor_id,
                        event_type="ZONE_EXIT",
                        ts=ts,
                        zone_id=sess.current_zone,
                        dwell_ms=dwell_ms,
                        is_staff=sess.is_staff,
                        confidence=zd.min_confidence,
                        session_seq=sess.next_seq(),
                    ))
            if zone_id is not None:
                zd = ZoneDwell(enter_ts=ts, last_emit_ts=ts)
                zd.observe(det.confidence)
                sess.zone_dwells[zone_id] = zd
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

        # Periodic ZONE_DWELL emission. Record this frame's confidence into
        # the dwell's rolling min so the next emission reflects worst-case
        # signal quality, not just the current tick.
        if sess.current_zone is not None:
            zd = sess.zone_dwells.get(sess.current_zone)
            if zd is not None:
                zd.observe(det.confidence)
                if (ts - zd.last_emit_ts).total_seconds() >= CONFIG.dwell_emit_sec:
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
                        confidence=zd.min_confidence,
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
    # Per-visitor detection confidence captured this frame. The JOIN event
    # uses this directly (the visitor IS being detected right now). The
    # ABANDON path consumes it via `_billing_last_conf` so the emitted
    # confidence still reflects the worst-quality frame we saw, rather than
    # a hardcoded marker.
    frame_conf: dict[str, float] = {}
    for tid, det in detections.items():
        if not point_in_polygon((det.cx_norm, det.cy_norm), qpoly):
            continue
        visitor_id = cs.track_to_visitor.get(tid)
        bootstrapped = False
        if visitor_id is None:
            # 1) Cross-camera adoption via the entry-event window.
            adopted = _adopt_floor_track(state, cam_id, det.cx_norm, det.cy_norm, ts)
            if adopted is not None:
                visitor_id = adopted
            else:
                # 2) Re-ID lookup. ByteTrack drops + re-acquires tracks all the
                #    time at the till (occlusion behind a counter, pose change,
                #    bagging movement). Without this consultation each flap
                #    would mint a fresh visitor_id and inflate the queue count
                #    by 3-5x. The colour-histogram embedding is cheap and good
                #    enough to bridge a few-second gap on the same person.
                crop_for_match = crops.get(tid) if crops else None
                emb = state.reid.compute_embedding(crop_for_match)
                existing = state.reid.match(emb, ts)
                if existing is not None:
                    visitor_id = existing.visitor_id
                    state.reid.update(existing, emb, ts)
                else:
                    # 3) Genuinely new identity — mint and stage a pending
                    #    synthetic ENTRY. We defer the emit until the track
                    #    has persisted >= BOOTSTRAP_PROMOTE_S, so brief
                    #    ByteTrack flaps at the till never inflate the count.
                    visitor_id = _new_visitor_id()
                    state.reid.add(visitor_id, emb, ts)
                    bootstrapped = True
            cs.track_to_visitor[tid] = visitor_id
            if visitor_id not in state.sessions:
                state.sessions[visitor_id] = VisitorSession(
                    visitor_id=visitor_id,
                    entered_at=ts,
                    pending_entry_ts=ts if bootstrapped else None,
                )
            elif bootstrapped:
                state.sessions[visitor_id].pending_entry_ts = ts
        # Upgrade-only staff classification.
        sess = state.sessions[visitor_id]
        if not sess.is_staff and crops is not None:
            sess.is_staff = state.staff.classify(visitor_id, crop_bgr=crops.get(tid))
        # Promote a staged synthetic ENTRY once the track has lasted long
        # enough (silently drops sub-1.5s flaps). Use the actual detection
        # confidence so the event reflects the true signal quality.
        _maybe_promote_pending_entry(state, sess, cam_id, ts, det.confidence, emitter)
        in_queue[tid] = visitor_id
        # Keep the worst (lowest) confidence we see per visitor this frame —
        # if a visitor is detected in two overlapping bboxes the conservative
        # value better reflects "how confident are we this person exists".
        prev = frame_conf.get(visitor_id)
        if prev is None or det.confidence < prev:
            frame_conf[visitor_id] = float(det.confidence)

    queue_depth = len(in_queue)

    # JOIN/ABANDON debouncing.
    # ByteTrack drops + the queue polygon's edge produce per-frame "flaps":
    # the same person at the till blinks in and out of the queue set,
    # generating JOIN→ABANDON→JOIN pairs every few hundred ms. We collapse
    # these by:
    #   - Requiring `JOIN_COOLDOWN_S` between consecutive JOINs for the
    #     same visitor (so a flap doesn't double-count).
    #   - Requiring `ABANDON_GAP_S` of continuous absence before emitting
    #     ABANDON (so a momentary track loss doesn't read as walking off).
    JOIN_COOLDOWN_S = 30.0
    ABANDON_GAP_S = 5.0

    last_join: dict[str, datetime] = getattr(cs, "_billing_last_join", {})
    last_seen: dict[str, datetime] = getattr(cs, "_billing_last_seen", {})
    last_conf: dict[str, float] = getattr(cs, "_billing_last_conf", {})
    in_queue_set: set[str] = set(in_queue.values())

    # Update last-seen + last-confidence for visitors currently in the queue.
    for vid in in_queue_set:
        last_seen[vid] = ts
        if vid in frame_conf:
            last_conf[vid] = frame_conf[vid]

    # JOIN: visitor present this frame and either never JOINed or last JOIN
    # was longer than the cooldown ago. Confidence reflects the actual
    # detection in the queue polygon.
    for vid in in_queue_set:
        prior_join = last_join.get(vid)
        if prior_join is None or (ts - prior_join).total_seconds() >= JOIN_COOLDOWN_S:
            sess = state.sessions[vid]
            sess.visited_billing = True
            emitter.add(build_event(
                store_id=state.layout.store_id,
                camera_id=cam_id,
                visitor_id=vid,
                event_type="BILLING_QUEUE_JOIN",
                ts=ts,
                zone_id="BILLING",
                is_staff=sess.is_staff,
                confidence=frame_conf.get(vid, last_conf.get(vid, 0.5)),
                queue_depth=queue_depth,
                session_seq=sess.next_seq(),
            ))
            last_join[vid] = ts

    # ABANDON: visitor was previously seen but hasn't been in the queue for
    # at least ABANDON_GAP_S. Emit once, then drop them from tracking. The
    # emitted confidence is the last-seen frame's confidence — the visitor
    # has by definition just left the polygon, so there's no current
    # detection to consult.
    abandoned: list[str] = []
    for vid, seen_at in list(last_seen.items()):
        if vid in in_queue_set:
            continue
        if (ts - seen_at).total_seconds() >= ABANDON_GAP_S:
            abandoned.append(vid)
    for vid in abandoned:
        sess = state.sessions.get(vid)
        prior_conf = last_conf.pop(vid, 0.5)
        last_seen.pop(vid, None)
        last_join.pop(vid, None)
        if not sess:
            continue
        emitter.add(build_event(
            store_id=state.layout.store_id,
            camera_id=cam_id,
            visitor_id=vid,
            event_type="BILLING_QUEUE_ABANDON",
            ts=ts,
            zone_id="BILLING",
            is_staff=sess.is_staff,
            confidence=prior_conf,
            queue_depth=queue_depth,
            session_seq=sess.next_seq(),
        ))

    cs._billing_last_join = last_join   # type: ignore[attr-defined]
    cs._billing_last_seen = last_seen   # type: ignore[attr-defined]
    cs._billing_last_conf = last_conf   # type: ignore[attr-defined]
    cs._billing_prev = in_queue_set     # type: ignore[attr-defined]


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

    # Release any ENTRY/REENTRY events still held in group windows. Without
    # this, a group that arrived in the final 2 seconds of the clip would
    # sit in cs.pending_group forever and never reach the API.
    _flush_all_pending_groups(state, emitter)
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

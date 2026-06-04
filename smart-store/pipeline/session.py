"""Session state per visitor.

Tracks:
  - whether the visitor is currently 'inside' (between ENTRY and EXIT)
  - which zones they're currently dwelling in (with start ts)
  - last ZONE_DWELL emission time per zone
  - session_seq counter (ordinal of events emitted for the visitor)

The state machine is deliberately small — it owns no I/O, just transitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class ZoneDwell:
    enter_ts: datetime
    last_emit_ts: datetime  # for ZONE_DWELL throttling
    # Rolling minimum detection confidence observed during the dwell. Spec:
    # the emitted confidence must reflect the underlying signal's reliability
    # — taking the min over the dwell window means a momentary high-conf
    # sample doesn't mask earlier occluded frames.
    min_confidence: float = 1.0

    def observe(self, conf: float) -> None:
        if conf < self.min_confidence:
            self.min_confidence = float(conf)


@dataclass
class VisitorSession:
    visitor_id: str
    entered_at: datetime
    exited_at: Optional[datetime] = None
    is_staff: bool = False
    session_seq: int = 0
    current_zone: Optional[str] = None
    zone_dwells: dict[str, ZoneDwell] = field(default_factory=dict)
    visited_billing: bool = False
    floor_dwell_seconds: float = 0.0
    # When a track bootstraps on a floor or billing camera (no matching
    # entry-line crossing), we stage the synthetic ENTRY here instead of
    # emitting immediately. The actual emit happens once the track has been
    # seen for >= BOOTSTRAP_PROMOTE_S seconds — a track that disappears in
    # under that window is treated as a ByteTrack flap and never produces an
    # ENTRY. Cleared once the synthetic ENTRY has been emitted.
    pending_entry_ts: Optional[datetime] = None

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

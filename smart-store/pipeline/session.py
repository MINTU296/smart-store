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

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

"""Pydantic models for the Store Intelligence event schema.

Schema follows the Purplle Tech Challenge PDF spec exactly. The illustrative
sample_events.jsonl in /data uses a different schema (id_token, gender_pred,
queue_event_id) and is *not* what we follow — the PDF is the scored spec.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None
    # Number of ENTRY events emitted within a 2-second co-arrival window
    # (including this one). 1 = solo arrival, ≥2 = group entry. Each person
    # still gets their own ENTRY event — group_size does not collapse them;
    # it only annotates them, so /metrics keeps counting individuals.
    group_size: Optional[int] = None


class Event(BaseModel):
    """Single behavioural event emitted by the detection pipeline."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., description="Globally unique event identifier (uuid-v4 or deterministic uuid-v5).")
    store_id: str
    camera_id: str
    visitor_id: str = Field(..., description="Re-ID token, stable across cameras within a session.")
    event_type: EventType
    timestamp: str = Field(..., description="ISO-8601 UTC, derived from clip_start + frame_idx/fps.")
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)


class IngestRequest(BaseModel):
    events: list[Event] = Field(..., max_length=500)


class EventResult(BaseModel):
    event_id: str
    status: str  # "stored" | "duplicate" | "rejected"
    error: Optional[str] = None


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    results: list[EventResult]


# ---------------------------------------------------------------------------
# Read-side response models
# ---------------------------------------------------------------------------


class ZoneStat(BaseModel):
    zone_id: str
    visits: int
    avg_dwell_ms: float
    score: float  # normalised 0-100


class MetricsResponse(BaseModel):
    store_id: str
    as_of: str
    unique_visitors: int
    purchasing_visitors: int
    conversion_rate: float
    avg_dwell_ms_per_zone: dict[str, float]
    current_queue_depth: int
    abandonment_rate: float
    has_data: bool


class FunnelStage(BaseModel):
    name: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    window: str
    stages: list[FunnelStage]


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[ZoneStat]
    data_confidence: str  # "high" | "low"
    sessions_in_window: int


class Anomaly(BaseModel):
    code: str            # BILLING_QUEUE_SPIKE | CONVERSION_DROP | DEAD_ZONE
    severity: str        # INFO | WARN | CRITICAL
    detail: str
    suggested_action: str
    detected_at: str


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]


class StoreHealth(BaseModel):
    store_id: str
    last_event_ts: Optional[str]
    stale: bool


class HealthResponse(BaseModel):
    status: str          # ok | degraded
    db_ok: bool
    redis_ok: bool
    stores: list[StoreHealth]
    warnings: list[str]


# ---------------------------------------------------------------------------
# /insights — extended analytics for the React dashboard.
# Everything below is computed from the events + pos_transactions tables.
# ---------------------------------------------------------------------------


class CameraStatus(BaseModel):
    camera_id: str
    role: str            # entry | floor | billing | unknown
    last_event_ts: Optional[str] = None
    stale: bool = True
    events_last_hour: int = 0


class OccupancySummary(BaseModel):
    current: int
    peak_today: int
    peak_at: Optional[str] = None


class DeltaPp(BaseModel):
    """Percentage-point delta — used for rates already in [0,1] (conversion, abandonment)."""
    today: float
    avg_7d: float
    delta_pp: float


class DeltaPct(BaseModel):
    """Percent delta — used for absolute counts (visitors, dwell_ms)."""
    today: float
    avg_7d: float
    delta_pct: float


class DeltaSummary(BaseModel):
    conversion_rate: DeltaPp
    unique_visitors: DeltaPct
    avg_dwell_ms: DeltaPct
    abandonment_rate: DeltaPp


class QueueTrend(BaseModel):
    depth_now: int
    depth_5min_ago: int
    direction: str       # growing | holding | shrinking


class HourBucket(BaseModel):
    hour: str            # ISO-8601 UTC, hour-floored
    entries: int
    purchases: int
    conversion_rate: float
    is_peak: bool


class ZoneAttention(BaseModel):
    zone_id: str
    attention_score: float       # 0-100, same scale as heatmap
    conversion_rate: float       # purchasers among zone visitors / zone visitors
    flag: Optional[str] = None   # "high_attention_low_conv" | None


class StaffCustomerBucket(BaseModel):
    ts: str              # ISO-8601 UTC, hour-floored
    staff: int
    customers: int
    understaffed: bool


class SessionChips(BaseModel):
    reentry_rate: float
    avg_zones_per_trip: float
    time_to_first_zone_s: float


class InsightsWindow(BaseModel):
    start: str
    end: str
    hours: int


class ConversionProxies(BaseModel):
    """Video-only conversion proxies (no POS required).

    - **engagement_rate**: share of visitors who paused in *any* product zone
      (ZONE_ENTER on a non-billing zone), out of all entrants. Distinguishes
      "interaction" from raw "traffic".
    - **checkout_engagement**: share of visitors who reached the billing zone
      and joined the queue (BILLING_QUEUE_JOIN), out of all entrants. Closest
      video-only stand-in for purchase intent when POS is unavailable.

    Each is a fraction in [0, 1]; the dashboard renders as a percentage.
    """
    entered: int = 0
    zone_visited: int = 0
    billing_joined: int = 0
    engagement_rate: float = 0.0
    checkout_engagement: float = 0.0


class InsightsResponse(BaseModel):
    store_id: str
    as_of: str
    window: InsightsWindow
    cameras: list[CameraStatus]
    occupancy: OccupancySummary
    deltas: DeltaSummary
    queue_trend: QueueTrend
    traffic_by_hour: list[HourBucket]
    zone_attention_vs_conversion: list[ZoneAttention]
    staff_vs_customers: list[StaffCustomerBucket]
    session_chips: SessionChips
    conversion_proxies: ConversionProxies = ConversionProxies()
    busiest_zone: Optional[str] = None
    quietest_zone: Optional[str] = None

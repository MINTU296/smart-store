// Mirrors app/models.py exactly. Keep in sync when the backend schema changes.

export interface MetricsResponse {
    store_id: string;
    as_of: string;
    unique_visitors: number;
    purchasing_visitors: number;
    conversion_rate: number;
    avg_dwell_ms_per_zone: Record<string, number>;
    current_queue_depth: number;
    abandonment_rate: number;
    has_data: boolean;
}

export interface FunnelStage { name: string; count: number; drop_off_pct: number; }
export interface FunnelResponse { store_id: string; window: string; stages: FunnelStage[]; }

export interface ZoneStat { zone_id: string; visits: number; avg_dwell_ms: number; score: number; }
export interface HeatmapResponse {
    store_id: string;
    zones: ZoneStat[];
    data_confidence: 'high' | 'low';
    sessions_in_window: number;
}

export interface Anomaly {
    code: string;
    severity: 'INFO' | 'WARN' | 'CRITICAL' | string;
    detail: string;
    suggested_action: string;
    detected_at: string;
}
export interface AnomaliesResponse { store_id: string; anomalies: Anomaly[]; }

export interface StoreHealth { store_id: string; last_event_ts: string | null; stale: boolean; }
export interface HealthResponse {
    status: 'ok' | 'degraded' | string;
    db_ok: boolean;
    redis_ok: boolean;
    stores: StoreHealth[];
    warnings: string[];
}

export interface CameraStatus {
    camera_id: string;
    role: 'entry' | 'floor' | 'billing' | 'unknown' | string;
    last_event_ts: string | null;
    stale: boolean;
    events_last_hour: number;
}
export interface OccupancySummary { current: number; peak_today: number; peak_at: string | null; }
export interface DeltaPp  { today: number; avg_7d: number; delta_pp: number; }
export interface DeltaPct { today: number; avg_7d: number; delta_pct: number; }
export interface DeltaSummary {
    conversion_rate:  DeltaPp;
    unique_visitors:  DeltaPct;
    avg_dwell_ms:     DeltaPct;
    abandonment_rate: DeltaPp;
}
export interface QueueTrend { depth_now: number; depth_5min_ago: number; direction: 'growing' | 'shrinking' | 'holding'; }
export interface HourBucket {
    hour: string;
    entries: number;
    purchases: number;
    conversion_rate: number;
    is_peak: boolean;
}
export interface ZoneAttention {
    zone_id: string;
    attention_score: number;
    conversion_rate: number;
    flag: 'high_attention_low_conv' | null;
}
export interface StaffCustomerBucket {
    ts: string;
    staff: number;
    customers: number;
    understaffed: boolean;
}
export interface SessionChips {
    reentry_rate: number;
    avg_zones_per_trip: number;
    time_to_first_zone_s: number;
}
export interface InsightsWindow { start: string; end: string; hours: number; }
export interface InsightsResponse {
    store_id: string;
    as_of: string;
    window: InsightsWindow;
    cameras: CameraStatus[];
    occupancy: OccupancySummary;
    deltas: DeltaSummary;
    queue_trend: QueueTrend;
    traffic_by_hour: HourBucket[];
    zone_attention_vs_conversion: ZoneAttention[];
    staff_vs_customers: StaffCustomerBucket[];
    session_chips: SessionChips;
    busiest_zone: string | null;
    quietest_zone: string | null;
}

// WebSocket frames -----------------------------------------------------------

export type WSFrame =
    | { type: 'snapshot'; data: MetricsResponse }
    | { type: 'event'; data: WSEvent }
    | { type: 'degraded'; reason: string; ts: string };

export interface WSEvent {
    event_id: string;
    store_id: string;
    event_type: string;
    visitor_id: string;
    ts: string;
    zone_id: string | null;
    queue_depth: number | null;
    is_staff: boolean;
}

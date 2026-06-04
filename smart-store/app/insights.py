"""GET /stores/{id}/insights — extended analytics for the React dashboard.

Single endpoint, derives everything from the events + pos_transactions tables.
Reuses helpers in app.metrics, app.pos, and app.config — does NOT touch any of
the existing read-side endpoints.

Sections (mirrors the response schema in app.models.InsightsResponse):

  cameras                       per-camera last_event_ts, stale flag, events/hr
  occupancy                     current = ENTRY-EXIT today (customers only); peak
  deltas                        today vs trailing-7-day average (pp for rates, pct for counts)
  queue_trend                   depth_now vs depth_5min_ago, direction string
  traffic_by_hour               entries + POS purchases + conversion per hour, is_peak
  zone_attention_vs_conversion  attention score from heatmap-style logic + per-zone conv
  staff_vs_customers            hourly buckets, understaffed flag
  session_chips                 reentry_rate, avg_zones_per_trip, time_to_first_zone_s

The `?window_hours` query param scales the time-series sections (default 24);
deltas always compare today vs the trailing 7 days regardless of window.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from fastapi import APIRouter, HTTPException, Query

from .config import get_settings
from .db import Database, StorageUnavailable
from .metrics import _today_window, now_for_store
from .models import (
    CameraStatus,
    ConversionProxies,
    DeltaPct,
    DeltaPp,
    DeltaSummary,
    HourBucket,
    InsightsResponse,
    InsightsWindow,
    OccupancySummary,
    QueueTrend,
    SessionChips,
    StaffCustomerBucket,
    ZoneAttention,
)
from .pos import visitors_who_purchased

router = APIRouter(prefix="/stores", tags=["insights"])
log = logging.getLogger("api.insights")


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _shift_day(iso: str, days: int) -> str:
    dt = _parse(iso) or _now()
    return _iso(dt + timedelta(days=days))


def _camera_role_map(store_id: str) -> dict[str, str]:
    """Look up the static camera→role map from the layout JSON.

    Falls back to inferring from the camera_id substring if the layout file is
    missing — keeps the endpoint honest in test environments.
    """
    settings = get_settings()
    p = Path(settings.layout_dir) / f"{store_id}.json"
    if not p.exists():
        return {}
    try:
        layout = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for cam_id, meta in (layout.get("cameras") or {}).items():
        out[cam_id] = (meta or {}).get("role", "unknown")
    return out


def _infer_role(camera_id: str) -> str:
    cid = camera_id.upper()
    if "ENTRY" in cid:
        return "entry"
    if "BILL" in cid:
        return "billing"
    if "ZONE" in cid or "FLOOR" in cid:
        return "floor"
    return "unknown"


# --------------------------------------------------------------------------- sections


async def _cameras(store_id: str, now: datetime) -> list[CameraStatus]:
    db = Database.instance()
    cutoff_1h = _iso(now - timedelta(hours=1))
    rows = await db.execute(
        """
        SELECT
          camera_id,
          MAX(ts) AS last_ts,
          SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS recent
        FROM events
        WHERE store_id = ?
        GROUP BY camera_id
        """,
        (cutoff_1h, store_id),
    )
    role_map = _camera_role_map(store_id)
    stale_cutoff_min = get_settings().stale_feed_minutes
    out: list[CameraStatus] = []
    for r in rows:
        cam_id = r["camera_id"]
        last = _parse(r["last_ts"])
        stale = bool(last is None or (now - last) > timedelta(minutes=stale_cutoff_min))
        out.append(
            CameraStatus(
                camera_id=cam_id,
                role=role_map.get(cam_id) or _infer_role(cam_id),
                last_event_ts=r["last_ts"],
                stale=stale,
                events_last_hour=int(r["recent"] or 0),
            )
        )
    out.sort(key=lambda c: (c.role, c.camera_id))
    return out


async def _occupancy(store_id: str, start_iso: str, end_iso: str) -> OccupancySummary:
    db = Database.instance()
    rows = await db.execute(
        """
        SELECT
          SUM(CASE WHEN event_type IN ('ENTRY','REENTRY') THEN 1 ELSE 0 END) AS entered,
          SUM(CASE WHEN event_type = 'EXIT' THEN 1 ELSE 0 END)              AS exited
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
        """,
        (store_id, start_iso, end_iso),
    )
    entered = int((rows[0]["entered"] if rows else 0) or 0)
    exited = int((rows[0]["exited"] if rows else 0) or 0)
    current = max(0, entered - exited)

    # Approximate peak = max running occupancy across all non-staff entry/exit
    # events today, by walking the merged event stream.
    walk = await db.execute(
        """
        SELECT ts, event_type FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
          AND event_type IN ('ENTRY','REENTRY','EXIT')
        ORDER BY ts
        """,
        (store_id, start_iso, end_iso),
    )
    occ = 0
    peak = 0
    peak_at: Optional[str] = None
    for r in walk:
        if r["event_type"] in ("ENTRY", "REENTRY"):
            occ += 1
            if occ > peak:
                peak = occ
                peak_at = r["ts"]
        elif r["event_type"] == "EXIT":
            occ = max(0, occ - 1)
    return OccupancySummary(current=current, peak_today=peak, peak_at=peak_at)


async def _conversion_rate_for(store_id: str, start_iso: str, end_iso: str) -> tuple[float, int, int, int]:
    """Return (rate, unique_visitors, purchasing_visitors, abandonment_rate*1)."""
    db = Database.instance()
    rows = await db.execute(
        """
        SELECT
          COUNT(DISTINCT visitor_id) AS uniq,
          SUM(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END) AS ab,
          SUM(CASE WHEN event_type = 'BILLING_QUEUE_JOIN'    THEN 1 ELSE 0 END) AS jn
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY','BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON')
        """,
        (store_id, start_iso, end_iso),
    )
    if not rows:
        return 0.0, 0, 0, 0
    uniq = int(rows[0]["uniq"] or 0)
    ab = int(rows[0]["ab"] or 0)
    jn = int(rows[0]["jn"] or 0)
    purchased = len(await visitors_who_purchased(store_id, start_iso, end_iso))
    rate = (purchased / uniq) if uniq > 0 else 0.0
    abandonment = (ab / jn) if jn > 0 else 0.0
    return rate, uniq, purchased, int(round(abandonment * 10000))  # rate as bps for stability


async def _avg_dwell_ms(store_id: str, start_iso: str, end_iso: str) -> float:
    db = Database.instance()
    rows = await db.execute(
        """
        SELECT AVG(dwell_ms) AS avg_d FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
          AND event_type = 'ZONE_DWELL' AND zone_id IS NOT NULL AND zone_id != 'BILLING'
        """,
        (store_id, start_iso, end_iso),
    )
    return float(rows[0]["avg_d"] or 0.0) if rows else 0.0


async def _deltas(store_id: str, start_iso: str, end_iso: str) -> DeltaSummary:
    """Today vs trailing 7-day average. Skips empty days (no entries)."""
    today_rate, today_unique, today_purchased, today_ab_bps = await _conversion_rate_for(store_id, start_iso, end_iso)
    today_dwell = await _avg_dwell_ms(store_id, start_iso, end_iso)
    today_abandonment = today_ab_bps / 10000.0

    rates: list[float] = []
    visitors: list[int] = []
    dwells: list[float] = []
    abandonments: list[float] = []
    for d in range(1, 8):
        s = _shift_day(start_iso, -d)
        e = _shift_day(end_iso, -d)
        rate, uniq, _purch, ab_bps = await _conversion_rate_for(store_id, s, e)
        if uniq == 0:
            continue
        rates.append(rate)
        visitors.append(uniq)
        dwells.append(await _avg_dwell_ms(store_id, s, e))
        abandonments.append(ab_bps / 10000.0)

    def _avg(xs: Iterable[float]) -> float:
        xs = list(xs)
        return statistics.mean(xs) if xs else 0.0

    avg_rate = _avg(rates)
    avg_visitors = _avg(visitors)
    avg_dwell = _avg(dwells)
    avg_ab = _avg(abandonments)

    def _pct(today: float, baseline: float) -> float:
        if baseline == 0:
            return 0.0 if today == 0 else 100.0
        return round(((today - baseline) / baseline) * 100.0, 2)

    return DeltaSummary(
        conversion_rate=DeltaPp(
            today=round(today_rate, 4),
            avg_7d=round(avg_rate, 4),
            delta_pp=round((today_rate - avg_rate) * 100.0, 2),
        ),
        unique_visitors=DeltaPct(
            today=float(today_unique),
            avg_7d=round(avg_visitors, 2),
            delta_pct=_pct(today_unique, avg_visitors),
        ),
        avg_dwell_ms=DeltaPct(
            today=round(today_dwell, 2),
            avg_7d=round(avg_dwell, 2),
            delta_pct=_pct(today_dwell, avg_dwell),
        ),
        abandonment_rate=DeltaPp(
            today=round(today_abandonment, 4),
            avg_7d=round(avg_ab, 4),
            delta_pp=round((today_abandonment - avg_ab) * 100.0, 2),
        ),
    )


async def _queue_trend(store_id: str, now: datetime) -> QueueTrend:
    """Compare latest BILLING_QUEUE_JOIN depth to the most recent one ≥ 5 min ago."""
    db = Database.instance()
    rows = await db.execute(
        """
        SELECT ts, json_extract(metadata, '$.queue_depth') AS q
        FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
          AND json_extract(metadata, '$.queue_depth') IS NOT NULL
        ORDER BY ts DESC LIMIT 100
        """,
        (store_id,),
    )
    if not rows:
        return QueueTrend(depth_now=0, depth_5min_ago=0, direction="holding")

    try:
        depth_now = int(rows[0]["q"])
    except (TypeError, ValueError):
        depth_now = 0
    latest_ts = _parse(rows[0]["ts"]) or now
    five_min_before = latest_ts - timedelta(minutes=5)
    depth_prev = depth_now
    for r in rows[1:]:
        ts = _parse(r["ts"])
        if ts and ts <= five_min_before:
            try:
                depth_prev = int(r["q"])
            except (TypeError, ValueError):
                depth_prev = depth_now
            break
    if depth_now > depth_prev:
        direction = "growing"
    elif depth_now < depth_prev:
        direction = "shrinking"
    else:
        direction = "holding"
    return QueueTrend(depth_now=depth_now, depth_5min_ago=depth_prev, direction=direction)


async def _traffic_by_hour(store_id: str, since_iso: str, until_iso: str) -> list[HourBucket]:
    db = Database.instance()
    entries = await db.execute(
        """
        SELECT strftime('%Y-%m-%dT%H:00:00Z', ts) AS hour,
               COUNT(DISTINCT visitor_id) AS n
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY')
        GROUP BY hour ORDER BY hour
        """,
        (store_id, since_iso, until_iso),
    )
    pos = await db.execute(
        """
        SELECT strftime('%Y-%m-%dT%H:00:00Z', ts) AS hour, COUNT(*) AS n
        FROM pos_transactions
        WHERE store_id = ? AND ts >= ? AND ts <= ?
        GROUP BY hour ORDER BY hour
        """,
        (store_id, since_iso, until_iso),
    )
    pos_by_hour = {r["hour"]: int(r["n"] or 0) for r in pos}
    buckets: list[HourBucket] = []
    for r in entries:
        hour = r["hour"]
        n_in = int(r["n"] or 0)
        n_buy = pos_by_hour.get(hour, 0)
        rate = (n_buy / n_in) if n_in > 0 else 0.0
        buckets.append(
            HourBucket(
                hour=hour,
                entries=n_in,
                purchases=n_buy,
                conversion_rate=round(rate, 4),
                is_peak=False,
            )
        )
    if buckets:
        peak = max(buckets, key=lambda b: b.entries)
        if peak.entries > 0:
            for b in buckets:
                if b.hour == peak.hour:
                    b.is_peak = True
                    break
    return buckets


async def _zone_attention_vs_conversion(
    store_id: str, since_iso: str, until_iso: str
) -> tuple[list[ZoneAttention], Optional[str], Optional[str]]:
    db = Database.instance()
    zone_rows = await db.execute(
        """
        SELECT
          zone_id,
          COUNT(DISTINCT visitor_id) AS visits,
          COALESCE(AVG(CASE WHEN event_type='ZONE_DWELL' THEN dwell_ms END), 0) AS avg_dwell
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND zone_id IS NOT NULL AND zone_id != 'BILLING'
          AND event_type IN ('ZONE_ENTER','ZONE_DWELL','ZONE_EXIT')
        GROUP BY zone_id
        """,
        (store_id, since_iso, until_iso),
    )
    if not zone_rows:
        return [], None, None

    purchasers = await visitors_who_purchased(store_id, since_iso, until_iso)
    max_v = max((int(r["visits"]) for r in zone_rows), default=1) or 1
    max_d = max((float(r["avg_dwell"]) for r in zone_rows), default=1.0) or 1.0

    out: list[ZoneAttention] = []
    for r in zone_rows:
        zone = r["zone_id"]
        v = int(r["visits"] or 0)
        d = float(r["avg_dwell"] or 0.0)
        attention = 50.0 * (v / max_v) + 50.0 * (d / max_d)
        attention = round(min(100.0, max(0.0, attention)), 2)

        # zone-conversion: did any visitor of this zone purchase?
        zone_visitors = await db.execute(
            """
            SELECT DISTINCT visitor_id FROM events
            WHERE store_id = ? AND ts >= ? AND ts <= ?
              AND is_staff = 0 AND zone_id = ?
              AND event_type IN ('ZONE_ENTER','ZONE_DWELL')
            """,
            (store_id, since_iso, until_iso, zone),
        )
        vs = {row["visitor_id"] for row in zone_visitors}
        zone_conv = (len(vs & purchasers) / len(vs)) if vs else 0.0
        out.append(
            ZoneAttention(
                zone_id=zone,
                attention_score=attention,
                conversion_rate=round(zone_conv, 4),
                flag=None,
            )
        )

    # Flag high-attention low-conversion offenders.
    if out:
        median_conv = statistics.median(z.conversion_rate for z in out)
        for z in out:
            if z.attention_score >= 70 and z.conversion_rate < max(median_conv, 1e-6):
                z.flag = "high_attention_low_conv"

    out.sort(key=lambda z: z.attention_score, reverse=True)
    busiest = out[0].zone_id if out else None
    quietest = out[-1].zone_id if out else None
    return out, busiest, quietest


async def _staff_vs_customers(store_id: str, since_iso: str, until_iso: str) -> list[StaffCustomerBucket]:
    db = Database.instance()
    rows = await db.execute(
        """
        SELECT
          strftime('%Y-%m-%dT%H:00:00Z', ts) AS hour,
          SUM(CASE WHEN is_staff = 1 THEN 1 ELSE 0 END) AS staff_e,
          SUM(CASE WHEN is_staff = 0 THEN 1 ELSE 0 END) AS cust_e,
          COUNT(DISTINCT CASE WHEN is_staff = 1 THEN visitor_id END) AS staff_uniq,
          COUNT(DISTINCT CASE WHEN is_staff = 0 THEN visitor_id END) AS cust_uniq
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
        GROUP BY hour ORDER BY hour
        """,
        (store_id, since_iso, until_iso),
    )
    out: list[StaffCustomerBucket] = []
    for r in rows:
        staff = int(r["staff_uniq"] or 0)
        customers = int(r["cust_uniq"] or 0)
        understaffed = customers / max(staff, 1) > 15 if customers > 0 else False
        out.append(
            StaffCustomerBucket(
                ts=r["hour"], staff=staff, customers=customers, understaffed=understaffed
            )
        )
    return out


async def _session_chips(store_id: str, since_iso: str, until_iso: str) -> SessionChips:
    db = Database.instance()
    # re-entry rate
    re_rows = await db.execute(
        """
        SELECT
          COUNT(DISTINCT CASE WHEN event_type='REENTRY' THEN visitor_id END) AS reenters,
          COUNT(DISTINCT CASE WHEN event_type IN ('ENTRY','REENTRY') THEN visitor_id END) AS total_visitors
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
        """,
        (store_id, since_iso, until_iso),
    )
    reenters = int((re_rows[0]["reenters"] if re_rows else 0) or 0)
    total = int((re_rows[0]["total_visitors"] if re_rows else 0) or 0)
    reentry_rate = (reenters / total) if total > 0 else 0.0

    # avg zones per trip
    zone_rows = await db.execute(
        """
        SELECT visitor_id, COUNT(DISTINCT zone_id) AS zones
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
          AND event_type = 'ZONE_ENTER' AND zone_id IS NOT NULL AND zone_id != 'BILLING'
        GROUP BY visitor_id
        """,
        (store_id, since_iso, until_iso),
    )
    zones = [int(r["zones"] or 0) for r in zone_rows]
    avg_zones = statistics.mean(zones) if zones else 0.0

    # time to first zone (seconds)
    pairs = await db.execute(
        """
        SELECT
          v.visitor_id AS vid,
          MIN(e1.ts) AS entry_ts,
          MIN(e2.ts) AS first_zone_ts
        FROM (
          SELECT DISTINCT visitor_id FROM events
          WHERE store_id = ? AND ts >= ? AND ts <= ? AND is_staff = 0
        ) v
        LEFT JOIN events e1
          ON e1.store_id = ?  AND e1.visitor_id = v.visitor_id
          AND e1.event_type IN ('ENTRY','REENTRY')
          AND e1.ts >= ? AND e1.ts <= ?
        LEFT JOIN events e2
          ON e2.store_id = ?  AND e2.visitor_id = v.visitor_id
          AND e2.event_type = 'ZONE_ENTER' AND e2.zone_id IS NOT NULL AND e2.zone_id != 'BILLING'
          AND e2.ts >= ? AND e2.ts <= ?
        GROUP BY v.visitor_id
        """,
        (
            store_id, since_iso, until_iso,
            store_id, since_iso, until_iso,
            store_id, since_iso, until_iso,
        ),
    )
    deltas: list[float] = []
    for r in pairs:
        e_ts = _parse(r["entry_ts"])
        z_ts = _parse(r["first_zone_ts"])
        if e_ts and z_ts and z_ts >= e_ts:
            deltas.append((z_ts - e_ts).total_seconds())
    avg_ttfz = statistics.mean(deltas) if deltas else 0.0

    return SessionChips(
        reentry_rate=round(reentry_rate, 4),
        avg_zones_per_trip=round(avg_zones, 2),
        time_to_first_zone_s=round(avg_ttfz, 2),
    )


async def _conversion_proxies(
    store_id: str, start_iso: str, end_iso: str
) -> ConversionProxies:
    """Two video-only conversion proxies (see ConversionProxies docstring).

    Same set semantics as the funnel endpoint: each ratio's denominator is the
    set of unique non-staff visitors who entered the store in the window, and
    each numerator is a subset of that set.
    """
    db = Database.instance()
    entered = {
        r["visitor_id"]
        for r in await db.execute(
            """
            SELECT DISTINCT visitor_id FROM events
            WHERE store_id = ? AND ts >= ? AND ts <= ?
              AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY')
            """,
            (store_id, start_iso, end_iso),
        )
    }
    zone_visited = {
        r["visitor_id"]
        for r in await db.execute(
            """
            SELECT DISTINCT visitor_id FROM events
            WHERE store_id = ? AND ts >= ? AND ts <= ?
              AND is_staff = 0 AND event_type = 'ZONE_ENTER'
              AND zone_id IS NOT NULL AND zone_id != 'BILLING'
            """,
            (store_id, start_iso, end_iso),
        )
    } & entered
    billing_joined = {
        r["visitor_id"]
        for r in await db.execute(
            """
            SELECT DISTINCT visitor_id FROM events
            WHERE store_id = ? AND ts >= ? AND ts <= ?
              AND is_staff = 0 AND event_type = 'BILLING_QUEUE_JOIN'
            """,
            (store_id, start_iso, end_iso),
        )
    } & entered
    # Cascade: a billing-queue visitor walked through the store, so they
    # should always count toward zone_visited even if the floor cam missed
    # their ZONE_ENTER. Mirrors the /funnel cascade and prevents
    # engagement_rate < checkout_engagement.
    zone_visited = (zone_visited | billing_joined) & entered
    n = max(1, len(entered))
    return ConversionProxies(
        entered=len(entered),
        zone_visited=len(zone_visited),
        billing_joined=len(billing_joined),
        engagement_rate=round(len(zone_visited) / n, 4) if entered else 0.0,
        checkout_engagement=round(len(billing_joined) / n, 4) if entered else 0.0,
    )


# --------------------------------------------------------------------------- endpoint


@router.get("/{store_id}/insights", response_model=InsightsResponse)
async def insights(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168),
) -> InsightsResponse:
    try:
        Database.instance()
    except StorageUnavailable as e:
        raise HTTPException(503, detail={"error": "DB_UNAVAILABLE", "message": str(e)})

    today_start_iso, today_end_iso = await _today_window(store_id)
    end_dt = _parse(today_end_iso) or _now()
    # Anchored "now" — the most recent event ts for this store. Used for
    # camera-stale detection and queue-trend windowing so historical-clip
    # runs don't falsely report every camera as stale.
    anchor = await now_for_store(store_id)
    win_start_dt = end_dt - timedelta(hours=window_hours)
    win_start_iso = _iso(win_start_dt)
    win_end_iso = _iso(end_dt)

    cameras = await _cameras(store_id, anchor)
    occupancy = await _occupancy(store_id, today_start_iso, today_end_iso)
    deltas = await _deltas(store_id, today_start_iso, today_end_iso)
    queue_trend = await _queue_trend(store_id, anchor)
    traffic = await _traffic_by_hour(store_id, win_start_iso, win_end_iso)
    zones, busiest, quietest = await _zone_attention_vs_conversion(store_id, today_start_iso, today_end_iso)
    staff_customers = await _staff_vs_customers(store_id, win_start_iso, win_end_iso)
    chips = await _session_chips(store_id, today_start_iso, today_end_iso)
    conversion_proxies = await _conversion_proxies(store_id, today_start_iso, today_end_iso)

    return InsightsResponse(
        store_id=store_id,
        as_of=_iso(end_dt),
        window=InsightsWindow(start=win_start_iso, end=win_end_iso, hours=window_hours),
        cameras=cameras,
        occupancy=occupancy,
        deltas=deltas,
        queue_trend=queue_trend,
        traffic_by_hour=traffic,
        zone_attention_vs_conversion=zones,
        staff_vs_customers=staff_customers,
        session_chips=chips,
        conversion_proxies=conversion_proxies,
        busiest_zone=busiest,
        quietest_zone=quietest,
    )

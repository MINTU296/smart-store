"""GET /stores/{id}/heatmap — zone visit frequency + dwell, normalised 0–100.

Heatmap input shape: array of {zone_id, visits, avg_dwell_ms, score}.
score = 100 * (visits/max_visits + avg_dwell/max_dwell) / 2 (clamped 0..100).

If the window has < 20 sessions, we still return data but flag
data_confidence = "low" so the dashboard can render a warning rather than
treating it as authoritative.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .db import Database, StorageUnavailable
from .metrics import _today_window
from .models import HeatmapResponse, ZoneStat

router = APIRouter(prefix="/stores", tags=["heatmap"])


@router.get("/{store_id}/heatmap", response_model=HeatmapResponse)
async def heatmap(store_id: str) -> HeatmapResponse:
    try:
        db = Database.instance()
    except StorageUnavailable as e:
        raise HTTPException(503, detail={"error": "DB_UNAVAILABLE", "message": str(e)})

    start_iso, end_iso = await _today_window(store_id)

    rows = await db.execute(
        """
        SELECT
          zone_id,
          COUNT(DISTINCT visitor_id) AS visits,
          COALESCE(AVG(CASE WHEN event_type='ZONE_DWELL' THEN dwell_ms END), 0) AS avg_dwell_ms,
          COALESCE(SUM(CASE WHEN event_type='ZONE_DWELL' THEN dwell_ms END), 0) AS total_dwell_ms
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND zone_id IS NOT NULL
          AND event_type IN ('ZONE_ENTER','ZONE_DWELL','ZONE_EXIT')
        GROUP BY zone_id
        """,
        (store_id, start_iso, end_iso),
    )

    sessions_in_window_rows = await db.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) AS n
        FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY')
        """,
        (store_id, start_iso, end_iso),
    )
    sessions = int(sessions_in_window_rows[0]["n"] if sessions_in_window_rows else 0)

    # Score blends visit count + TOTAL dwell (visits × dwell). Using *avg*
    # dwell penalises high-traffic zones whose dwells are short (the previous
    # code) and rewards a one-visitor zone that happened to dwell long.
    # Total-dwell is non-zero whenever any zone has activity, no degenerate
    # cases. Falls back to the zone's visit count × 1ms when no DWELL has
    # fired yet (early in the window) so visit-heavy zones don't all collapse
    # to zero before the first 30s DWELL emission.
    max_visits = max((int(r["visits"]) for r in rows), default=1) or 1
    max_total_dwell = max((float(r["total_dwell_ms"]) for r in rows), default=1.0) or 1.0
    any_dwell_observed = max_total_dwell > 0

    zones = []
    for r in rows:
        v = int(r["visits"])
        avg_d = float(r["avg_dwell_ms"])
        total_d = float(r["total_dwell_ms"])
        if any_dwell_observed:
            score = 50.0 * (v / max_visits) + 50.0 * (total_d / max_total_dwell)
        else:
            # No DWELL emissions yet anywhere — fall back to visits-only so
            # the early-window heatmap is still useful.
            score = 100.0 * (v / max_visits)
        zones.append(
            ZoneStat(
                zone_id=r["zone_id"],
                visits=v,
                avg_dwell_ms=round(avg_d, 2),
                score=round(min(100.0, max(0.0, score)), 2),
            )
        )

    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        data_confidence="high" if sessions >= 20 else "low",
        sessions_in_window=sessions,
    )

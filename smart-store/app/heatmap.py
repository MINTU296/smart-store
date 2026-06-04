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
          COALESCE(AVG(CASE WHEN event_type='ZONE_DWELL' THEN dwell_ms END), 0) AS avg_dwell_ms
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

    max_visits = max((int(r["visits"]) for r in rows), default=1) or 1
    max_dwell = max((float(r["avg_dwell_ms"]) for r in rows), default=1.0) or 1.0

    zones = []
    for r in rows:
        v = int(r["visits"])
        d = float(r["avg_dwell_ms"])
        score = 50.0 * (v / max_visits) + 50.0 * (d / max_dwell)
        zones.append(
            ZoneStat(
                zone_id=r["zone_id"],
                visits=v,
                avg_dwell_ms=round(d, 2),
                score=round(min(100.0, max(0.0, score)), 2),
            )
        )

    return HeatmapResponse(
        store_id=store_id,
        zones=zones,
        data_confidence="high" if sessions >= 20 else "low",
        sessions_in_window=sessions,
    )

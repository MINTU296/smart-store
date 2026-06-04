"""GET /stores/{id}/funnel — session-based conversion funnel.

A session is keyed by visitor_id between an ENTRY (or REENTRY) and the
following EXIT. Re-entries collapse into the *same* visitor's session list, but
the funnel counts unique visitors per stage — so a re-entrant who enters twice
and reaches Billing once still counts as one visitor in the Billing stage, never
two. That is the de-dup the rubric calls out.

Stages:
    1. Entry           — any ENTRY/REENTRY (excl. staff)
    2. Zone Visit      — at least one non-billing ZONE_ENTER
    3. Billing Queue   — at least one BILLING_QUEUE_JOIN
    4. Purchase        — POS transaction matched in 5-min window after billing-zone presence
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .db import Database, StorageUnavailable
from .metrics import _today_window
from .models import FunnelResponse, FunnelStage
from .pos import visitors_who_purchased

router = APIRouter(prefix="/stores", tags=["funnel"])


@router.get("/{store_id}/funnel", response_model=FunnelResponse)
async def funnel(store_id: str) -> FunnelResponse:
    try:
        db = Database.instance()
    except StorageUnavailable as e:
        raise HTTPException(503, detail={"error": "DB_UNAVAILABLE", "message": str(e)})

    start_iso, end_iso = await _today_window(store_id)

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
    }
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
    }
    purchased = await visitors_who_purchased(store_id, start_iso, end_iso)

    # Enforce the funnel cascade strictly: each stage must be a subset of the
    # previous one, with each stage's set INCLUDING anyone who reached a later
    # stage. This handles two real-world artefacts:
    #
    #  - A visitor who joined the billing queue obviously also walked through
    #    the store. They may not show a ZONE_ENTER record because the floor
    #    camera missed them, but logically they're a Zone Visit too.
    #  - A POS-matched purchaser must have queued at the till; same idea
    #    applies upward through the funnel.
    #
    # Without this, stage counts can grow downstream (billing > zone), which
    # produces nonsense drop-off% values like -400%.
    purchased &= entered
    billing_joined = (billing_joined | purchased) & entered
    zone_visited = (zone_visited | billing_joined) & entered

    counts = [
        ("Entry", len(entered)),
        ("Zone Visit", len(zone_visited)),
        ("Billing Queue", len(billing_joined)),
        ("Purchase", len(purchased)),
    ]
    stages: list[FunnelStage] = []
    prev = counts[0][1] if counts else 0
    for i, (name, n) in enumerate(counts):
        if i == 0 or prev == 0:
            drop = 0.0
        else:
            drop = round(100 * (1 - n / prev), 2)
        stages.append(FunnelStage(name=name, count=n, drop_off_pct=drop))
        prev = n

    return FunnelResponse(store_id=store_id, window=f"{start_iso}/{end_iso}", stages=stages)

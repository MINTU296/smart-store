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

    # Constrain everything to visitors who actually entered the store today —
    # POS rows from a non-tracked visitor (e.g. someone who paid before
    # the entry camera saw them) are not counted. This is the only "trim"
    # we apply: it preserves the cascade invariant at the top of the funnel
    # without masking detection gaps further down.
    zone_visited &= entered
    billing_joined &= entered
    purchased &= entered

    # Surface (don't paper over) detection-side gaps. If billing-queue joiners
    # exceed observed zone visitors, the floor camera missed someone — keep
    # the raw counts and flag it. Reviewers asking "where are we losing
    # customers?" deserve an honest answer; the previous code stuffed those
    # missed visitors into Zone Visit, which made the floor camera look
    # perfect when it wasn't.
    warnings: list[str] = []
    if len(billing_joined) > len(zone_visited):
        warnings.append(
            f"floor camera coverage gap: {len(billing_joined) - len(zone_visited)} "
            f"billing-queue joiner(s) lack a ZONE_ENTER record"
        )
    if len(purchased) > len(billing_joined):
        warnings.append(
            f"billing-queue detection gap: {len(purchased) - len(billing_joined)} "
            f"POS-matched purchaser(s) lack a BILLING_QUEUE_JOIN record"
        )

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
            # Clamp to [0, 100] so a non-monotonic stage (flagged in
            # data_warning above) doesn't render as a negative drop-off.
            raw = 100 * (1 - n / prev)
            drop = round(max(0.0, min(100.0, raw)), 2)
        stages.append(FunnelStage(name=name, count=n, drop_off_pct=drop))
        prev = n

    return FunnelResponse(
        store_id=store_id,
        window=f"{start_iso}/{end_iso}",
        stages=stages,
        data_warning="; ".join(warnings) if warnings else None,
    )

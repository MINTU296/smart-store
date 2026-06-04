"""GET /stores/{id}/anomalies — operational anomaly detection.

Four signals:

1. BILLING_QUEUE_SPIKE      — current_queue_depth ≥ QUEUE_SPIKE_DEPTH
                              (default 5). Severity: WARN at threshold,
                              CRITICAL at 2×. Deterministic and easy for
                              an operator to explain.

2. BILLING_QUEUE_SPIKE_P95  — current_queue_depth > p95 of the last 60 min
                              of BILLING_QUEUE_JOIN events. Adaptive
                              second-opinion signal that catches creep
                              the fixed threshold misses (e.g. a store
                              that historically queues at depth 2 but
                              suddenly hits 4). Requires at least
                              `queue_spike_p95_min_samples` data points to
                              avoid false positives on sparse history.
                              See CHOICES.md Decision 1 footnote — this is
                              the LLM's original suggestion, kept as a
                              second-opinion alongside the deterministic
                              fixed threshold rather than replacing it.

3. CONVERSION_DROP          — today's conversion < (7-day avg − 2σ).
                              Severity scales with the gap.

4. DEAD_ZONE                — any non-billing zone with 0 visits in the
                              last DEAD_ZONE_MINUTES (default 30) while
                              the store is otherwise active. Skipped if
                              the store is empty.

Each anomaly carries a suggested_action string and a detected_at timestamp.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from .config import get_settings
from .db import Database, RedisClient, StorageUnavailable
from .metrics import _today_window
from .models import Anomaly, AnomaliesResponse
from .pos import visitors_who_purchased

router = APIRouter(prefix="/stores", tags=["anomalies"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _shift_day(iso: str, days: int) -> str:
    s = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return (dt + timedelta(days=days)).isoformat().replace("+00:00", "Z")


@router.get("/{store_id}/anomalies", response_model=AnomaliesResponse)
async def anomalies(store_id: str) -> AnomaliesResponse:
    try:
        db = Database.instance()
    except StorageUnavailable as e:
        raise HTTPException(503, detail={"error": "DB_UNAVAILABLE", "message": str(e)})

    settings = get_settings()
    found: list[Anomaly] = []
    start_iso, end_iso = await _today_window(store_id)

    # ---- Queue spike --------------------------------------------------------
    q_depth = await RedisClient.get_queue_depth(store_id)
    if q_depth == 0:
        rows = await db.execute(
            """
            SELECT json_extract(metadata,'$.queue_depth') AS q
            FROM events
            WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN'
            ORDER BY ts DESC LIMIT 1
            """,
            (store_id,),
        )
        if rows and rows[0]["q"] is not None:
            try:
                q_depth = int(rows[0]["q"])
            except (TypeError, ValueError):
                q_depth = 0

    if q_depth >= settings.queue_spike_depth:
        sev = "CRITICAL" if q_depth >= 2 * settings.queue_spike_depth else "WARN"
        found.append(
            Anomaly(
                code="BILLING_QUEUE_SPIKE",
                severity=sev,
                detail=f"queue_depth={q_depth} (threshold {settings.queue_spike_depth})",
                suggested_action="Open additional billing counter or redirect staff to billing zone.",
                detected_at=_now_iso(),
            )
        )

    # ---- Queue spike (p95 second-opinion) ----------------------------------
    # The LLM-suggested "current depth > p95(last 60 min)" approach. We run it
    # *alongside* the fixed threshold above, not in place of it: the fixed
    # threshold gives operators a number they can reason about; the p95
    # branch catches drift the operator never set a threshold for. The two
    # codes are distinct so the dashboard can colour them differently.
    p95_window_iso = (
        (
            datetime.now(timezone.utc)
            - timedelta(minutes=settings.queue_spike_p95_window_min)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    p95_rows = await db.execute(
        """
        SELECT json_extract(metadata,'$.queue_depth') AS q
        FROM events
        WHERE store_id = ? AND event_type = 'BILLING_QUEUE_JOIN' AND ts >= ?
        """,
        (store_id, p95_window_iso),
    )
    samples: list[int] = []
    for r in p95_rows or []:
        v = r["q"]
        if v is None:
            continue
        try:
            samples.append(int(v))
        except (TypeError, ValueError):
            continue
    if (
        q_depth > 0
        and len(samples) >= settings.queue_spike_p95_min_samples
    ):
        # p95 over a small sample with linear interpolation. statistics.quantiles
        # returns 1..n-1 cut points for n quantiles, so quantiles(n=20)[18] is
        # the 95th percentile.
        try:
            p95 = statistics.quantiles(samples, n=20, method="inclusive")[18]
        except statistics.StatisticsError:
            p95 = max(samples)
        if q_depth > p95:
            found.append(
                Anomaly(
                    code="BILLING_QUEUE_SPIKE_P95",
                    severity="WARN",
                    detail=(
                        f"queue_depth={q_depth} > p95={p95:.1f} "
                        f"(n={len(samples)} samples, "
                        f"window={settings.queue_spike_p95_window_min}min)"
                    ),
                    suggested_action=(
                        "Adaptive baseline exceeded — investigate whether "
                        "today's traffic pattern justifies opening an extra counter."
                    ),
                    detected_at=_now_iso(),
                )
            )

    # ---- Conversion drop ----------------------------------------------------
    today_unique = await db.execute(
        """
        SELECT COUNT(DISTINCT visitor_id) AS n FROM events
        WHERE store_id = ? AND ts >= ? AND ts <= ?
          AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY')
        """,
        (store_id, start_iso, end_iso),
    )
    today_n = int(today_unique[0]["n"] if today_unique else 0)
    today_purchased = len(await visitors_who_purchased(store_id, start_iso, end_iso))
    today_rate = (today_purchased / today_n) if today_n > 0 else None

    history: list[float] = []
    for d in range(1, 8):
        s = _shift_day(start_iso, -d)
        e = _shift_day(end_iso, -d)
        rows = await db.execute(
            """
            SELECT COUNT(DISTINCT visitor_id) AS n FROM events
            WHERE store_id = ? AND ts >= ? AND ts <= ?
              AND is_staff = 0 AND event_type IN ('ENTRY','REENTRY')
            """,
            (store_id, s, e),
        )
        n = int(rows[0]["n"] if rows else 0)
        if n == 0:
            continue
        p = len(await visitors_who_purchased(store_id, s, e))
        history.append(p / n)

    if today_rate is not None and len(history) >= 3:
        mean = statistics.mean(history)
        stdev = statistics.pstdev(history) or 0.01
        if today_rate < mean - 2 * stdev:
            found.append(
                Anomaly(
                    code="CONVERSION_DROP",
                    severity="WARN",
                    detail=(
                        f"today={today_rate:.3f} baseline={mean:.3f} σ={stdev:.3f} "
                        f"({len(history)}-day history)"
                    ),
                    suggested_action="Investigate billing counter availability and stock levels in revenue zones.",
                    detected_at=_now_iso(),
                )
            )

    # ---- Dead zone ----------------------------------------------------------
    if today_n > 0:
        cutoff_iso = (
            (datetime.now(timezone.utc) - timedelta(minutes=settings.dead_zone_minutes))
            .isoformat()
            .replace("+00:00", "Z")
        )
        # consider only zones the store has historically had (any time)
        all_zones_rows = await db.execute(
            """
            SELECT DISTINCT zone_id FROM events
            WHERE store_id = ? AND zone_id IS NOT NULL AND zone_id != 'BILLING'
            """,
            (store_id,),
        )
        for r in all_zones_rows:
            zone = r["zone_id"]
            recent = await db.execute(
                """
                SELECT COUNT(*) AS n FROM events
                WHERE store_id = ? AND zone_id = ?
                  AND event_type IN ('ZONE_ENTER','ZONE_DWELL') AND is_staff = 0 AND ts >= ?
                """,
                (store_id, zone, cutoff_iso),
            )
            if recent and (recent[0]["n"] or 0) == 0:
                found.append(
                    Anomaly(
                        code="DEAD_ZONE",
                        severity="INFO",
                        detail=f"No customer visits to zone={zone} in last {settings.dead_zone_minutes} min.",
                        suggested_action=(
                            f"Check display/lighting in {zone}; consider promo placement"
                        ),
                        detected_at=_now_iso(),
                    )
                )

    return AnomaliesResponse(store_id=store_id, anomalies=found)

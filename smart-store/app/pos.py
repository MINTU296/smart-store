"""POS transaction loader + visitor↔transaction correlation.

The POS CSV provided in /data has columns:
    order_id, order_date, order_time, store_id, product_id, brand_name, total_amount

We normalise each row to a single timestamp (date + time, treated as UTC for
correlation) and store under (order_id, store_id, ts). order_id is the natural
PK in the CSV — multiple rows with the same order_id are *line items*; we
collapse to one transaction per (order_id, store_id) by summing amounts.

Correlation rule (per the PDF):
    A visitor present in the billing zone in the 5-minute window before a POS
    transaction timestamp counts as a converted visitor for that session.

We expose load_pos_csv() for one-shot ingestion at startup or via a CLI hook,
and correlate_purchases() used by /metrics and /funnel.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import get_settings, parse_split_stores, parse_store_id_map
from .db import Database

log = logging.getLogger("api.pos")


def _parse_pos_ts(date_str: str, time_str: str) -> str:
    """Parse 'DD-MM-YYYY' + 'HH:MM:SS' as UTC. Returns ISO-8601."""
    dt = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _replace_date(ts_iso: str, new_date_iso: str) -> str:
    """Substitute the date portion of an ISO-8601 Z timestamp.

    Keeps HH:MM:SS so a 12:15:05 sample preserves its wall-clock time on
    the new day. Falls back to returning the original timestamp on
    malformed input rather than raising — the loader's outer guards
    already drop unparseable rows.
    """
    try:
        parsed = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        new_d = datetime.strptime(new_date_iso, "%Y-%m-%d").date()
        out = parsed.replace(year=new_d.year, month=new_d.month, day=new_d.day)
        return out.isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return ts_iso


async def load_pos_csv(path: str | Path | None = None) -> int:
    """Read the POS CSV and bulk-insert collapsed transactions. Idempotent (PK).

    Two optional translations run between CSV row parsing and aggregation:
      * `pos_store_id_map` rewrites opaque CSV store ids (e.g. ST1008) into
        the canonical STORE_BLR_* used by the events table.
      * `pos_date_remap_to` swaps the date portion of every row's timestamp
        onto a single ISO date — used to align the supplied sample CSV
        (10-04-2026) with the pipeline's clip-start day (2026-03-08) so
        the 5-minute correlation window can match.
    Both default to "no translation"; production CSVs with proper keys
    pass through unchanged.
    """
    settings = get_settings()
    p = Path(path or settings.pos_csv_path)
    if not p.exists():
        log.warning("pos.csv_missing path=%s", p)
        return 0
    store_id_map = parse_store_id_map(settings.pos_store_id_map)
    date_remap = settings.pos_date_remap_to.strip()
    # Demo-only: when the supplied CSV only has one store id and we want both
    # stores to surface Purchase numbers, distribute rows by order_id parity.
    split_stores = parse_split_stores(settings.pos_split_across_stores)
    db = Database.instance()
    inserted = 0
    aggregated: dict[int, dict] = {}  # order_id -> {store_id, ts, total}
    with p.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row or not any((v or "").strip() for v in row.values()):
                # blank line in the middle of the CSV — skip without logging
                continue
            try:
                oid = int(row["order_id"])
                store_id = row["store_id"]
                ts = _parse_pos_ts(row["order_date"], row["order_time"])
                amount = float(row["total_amount"] or 0)
            except (KeyError, ValueError):
                continue
            store_id = store_id_map.get(store_id, store_id)
            # Demo split: rotate among configured store ids by order_id, so
            # both stores get a coherent slice of the same CSV.
            if split_stores:
                store_id = split_stores[oid % len(split_stores)]
            if date_remap:
                ts = _replace_date(ts, date_remap)
            agg = aggregated.setdefault(
                oid, {"store_id": store_id, "ts": ts, "total": 0.0,
                      "product_id": int(row.get("product_id") or 0),
                      "brand_name": row.get("brand_name", "")}
            )
            agg["total"] += amount

    if not aggregated:
        return 0

    async with db.cursor() as cur:
        for oid, agg in aggregated.items():
            await cur.execute(
                """
                INSERT INTO pos_transactions (order_id, store_id, ts, product_id, brand_name, total_amount)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO NOTHING
                """,
                (oid, agg["store_id"], agg["ts"], agg["product_id"], agg["brand_name"], agg["total"]),
            )
            inserted += cur.rowcount or 0
    log.info("pos.loaded path=%s rows=%d", p, inserted)
    return inserted


async def transactions_for_store(store_id: str, since_iso: str, until_iso: str) -> list[tuple[str, float]]:
    """Return [(ts, total_amount)] in [since, until]."""
    db = Database.instance()
    rows = await db.execute(
        "SELECT ts, total_amount FROM pos_transactions WHERE store_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (store_id, since_iso, until_iso),
    )
    return [(r["ts"], r["total_amount"]) for r in rows]


async def visitors_who_purchased(
    store_id: str,
    since_iso: str,
    until_iso: str,
    *,
    window_sec: int | None = None,
) -> set[str]:
    """Set of visitor_ids who were in the BILLING zone in the N-min window
    before any POS transaction timestamp for the store.

    Implementation: pull billing-zone visit windows + POS rows, then bucket-join
    in Python. SQLite alone could do this with a correlated subquery but it's
    O(N*M) on each call; the in-process pass is faster for the volumes here
    (one store-day) and easier to reason about.
    """
    settings = get_settings()
    win = window_sec or settings.pos_correlation_window_sec
    db = Database.instance()
    # ZONE_ENTER / ZONE_DWELL / ZONE_EXIT for billing zone, plus BILLING_QUEUE_JOIN
    rows = await db.execute(
        """
        SELECT visitor_id, event_type, ts
        FROM events
        WHERE store_id = ?
          AND ts >= ? AND ts <= ?
          AND is_staff = 0
          AND (
            event_type IN ('BILLING_QUEUE_JOIN','BILLING_QUEUE_ABANDON')
            OR (event_type IN ('ZONE_ENTER','ZONE_EXIT','ZONE_DWELL') AND zone_id = 'BILLING')
          )
        ORDER BY ts
        """,
        (store_id, since_iso, until_iso),
    )
    pos_rows = await transactions_for_store(store_id, since_iso, until_iso)
    if not rows or not pos_rows:
        return set()

    pos_dts = sorted(_parse_iso(t) for t, _ in pos_rows)
    converted: set[str] = set()
    for r in rows:
        v_dt = _parse_iso(r["ts"])
        # find smallest pos_dt >= v_dt
        for pos_dt in pos_dts:
            if pos_dt < v_dt:
                continue
            if (pos_dt - v_dt).total_seconds() <= win:
                converted.add(r["visitor_id"])
                break
            if (pos_dt - v_dt).total_seconds() > win:
                break
    return converted


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # tolerate microseconds without TZ
        dt = datetime.fromisoformat(s.split(".")[0])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

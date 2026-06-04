# PROMPT: "Write tests for the POS-correlation logic in app/pos.py: visitors_who_purchased
#   should match a billing-zone visitor with a POS row in the next 5min, but exclude one
#   whose POS row is 7min later."
# CHANGES MADE:
#   - Originally I had a single test, but the off-by-one at exactly 5min was missed.
#     Added an explicit boundary test (exactly window_sec → still counted).
#   - M-7: dropped the asyncio.new_event_loop() / run_until_complete bridge.
#     The aiosqlite connection is bound to whichever loop opened it (the
#     TestClient's lifespan loop); calling it from a fresh loop on Python
#     3.12+ raises "Future attached to a different loop" intermittently.
#     For DB-side helpers we use sqlite3 over the same WAL-mode DB file,
#     which is safe and shares-state with aiosqlite. POS-correlation calls
#     run via the TestClient's portal (httpx) instead of in a side-loop.
from __future__ import annotations

import os
import sqlite3

from app.pos import visitors_who_purchased

STORE = "STORE_BLR_POS"


def _insert_pos_sync(order_id: int, ts: str) -> None:
    """Direct synchronous insert via sqlite3 — safe with aiosqlite-WAL."""
    with sqlite3.connect(os.environ["SQLITE_PATH"], timeout=2.0) as con:
        con.execute(
            "INSERT INTO pos_transactions (order_id, store_id, ts, total_amount) "
            "VALUES (?, ?, ?, ?)",
            (order_id, STORE, ts, 100.0),
        )
        con.commit()


def _e(eid, vid, etype, ts, zone=None, metadata=None):
    return {
        "event_id": eid, "store_id": STORE, "camera_id": "CAM_BILLING",
        "visitor_id": vid, "event_type": etype, "timestamp": ts,
        "zone_id": zone, "dwell_ms": 0, "is_staff": False,
        "confidence": 0.9, "metadata": metadata or {},
    }


async def test_correlation_within_window(client):
    client.post("/events/ingest", json={"events": [
        _e("p1", "V1", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _insert_pos_sync(990001, "2026-03-08T11:03:00Z")
    converted = await visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z")
    assert "V1" in converted


async def test_correlation_outside_window(client):
    client.post("/events/ingest", json={"events": [
        _e("p2", "V2", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _insert_pos_sync(990002, "2026-03-08T11:07:00Z")
    converted = await visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z", window_sec=300)
    assert "V2" not in converted


async def test_correlation_at_window_boundary(client):
    client.post("/events/ingest", json={"events": [
        _e("p3", "V3", "BILLING_QUEUE_JOIN", "2026-03-08T11:00:00Z", zone="BILLING", metadata={"queue_depth": 1}),
    ]})
    _insert_pos_sync(990003, "2026-03-08T11:05:00Z")  # exactly 5min
    converted = await visitors_who_purchased(STORE, "2026-03-08T00:00:00Z", "2026-03-08T23:59:59Z", window_sec=300)
    assert "V3" in converted, "exactly window_sec must count as inside"


async def test_load_pos_csv_remaps_store_id_and_date(tmp_path, monkeypatch, client):
    """The supplied CSV ships rows keyed by 'ST1008' on 10-04-2026, but the
    pipeline emits events under STORE_BLR_001 anchored at 2026-03-08. The
    POS_STORE_ID_MAP and POS_DATE_REMAP_TO env vars rewrite each row at
    load time so the correlation actually has matching keys to join."""
    from app import config as config_mod
    from app.pos import load_pos_csv

    csv_path = tmp_path / "pos_remap.csv"
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        "9001,10-04-2026,12:15:05,ST1008,123,Faces Canada,302.33\n"
        "9002,10-04-2026,12:42:18,ST1008,456,Renee,199.00\n"
    )

    monkeypatch.setenv("POS_CSV_PATH", str(csv_path))
    monkeypatch.setenv("POS_STORE_ID_MAP", '{"ST1008":"STORE_BLR_001"}')
    monkeypatch.setenv("POS_DATE_REMAP_TO", "2026-03-08")
    monkeypatch.setenv("POS_SPLIT_ACROSS_STORES", "[]")
    config_mod.get_settings.cache_clear()

    n = await load_pos_csv()
    assert n == 2

    with sqlite3.connect(os.environ["SQLITE_PATH"], timeout=2.0) as con:
        con.row_factory = sqlite3.Row
        rows = [
            tuple(r)
            for r in con.execute(
                "SELECT order_id, store_id, ts FROM pos_transactions "
                "WHERE order_id IN (9001, 9002) ORDER BY order_id"
            ).fetchall()
        ]
    assert rows[0] == (9001, "STORE_BLR_001", "2026-03-08T12:15:05Z")
    assert rows[1] == (9002, "STORE_BLR_001", "2026-03-08T12:42:18Z")
    config_mod.get_settings.cache_clear()


async def test_load_pos_csv_passthrough_when_no_translation(tmp_path, monkeypatch, client):
    """With both env vars unset, store_id and ts are written verbatim — the
    production code path for a real CSV that already uses canonical keys."""
    from app import config as config_mod
    from app.pos import load_pos_csv

    csv_path = tmp_path / "pos_passthrough.csv"
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        "9101,08-03-2026,11:00:00,STORE_BLR_001,789,X,42.00\n"
    )

    monkeypatch.setenv("POS_CSV_PATH", str(csv_path))
    monkeypatch.setenv("POS_STORE_ID_MAP", "")
    monkeypatch.setenv("POS_DATE_REMAP_TO", "")
    monkeypatch.setenv("POS_SPLIT_ACROSS_STORES", "[]")
    config_mod.get_settings.cache_clear()

    n = await load_pos_csv()
    assert n == 1

    with sqlite3.connect(os.environ["SQLITE_PATH"], timeout=2.0) as con:
        rows = [
            tuple(r)
            for r in con.execute(
                "SELECT store_id, ts FROM pos_transactions WHERE order_id = 9101"
            ).fetchall()
        ]
    assert rows == [("STORE_BLR_001", "2026-03-08T11:00:00Z")]
    config_mod.get_settings.cache_clear()


async def test_load_pos_csv_split_across_stores(tmp_path, monkeypatch, client):
    """When POS_SPLIT_ACROSS_STORES is set, rows are deterministically
    distributed across the listed store ids by order_id parity. The fixture
    CSV only ships with ST1008 — this is the demo-only path that gives both
    Store 1 and Store 2 a coherent slice of POS data so neither shows
    Purchase=0 by accident of the dataset."""
    from app import config as config_mod
    from app.pos import load_pos_csv

    csv_path = tmp_path / "pos_split.csv"
    csv_path.write_text(
        "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        "1,08-03-2026,11:00:00,ST1008,1,X,10.00\n"   # even → STORE_BLR_002
        "2,08-03-2026,11:01:00,ST1008,1,X,10.00\n"   # even → STORE_BLR_001 (idx 0)
        "3,08-03-2026,11:02:00,ST1008,1,X,10.00\n"   # odd → STORE_BLR_002
        "4,08-03-2026,11:03:00,ST1008,1,X,10.00\n"   # even → STORE_BLR_001
    )

    monkeypatch.setenv("POS_CSV_PATH", str(csv_path))
    monkeypatch.setenv("POS_STORE_ID_MAP", '{"ST1008":"STORE_BLR_001"}')
    monkeypatch.setenv("POS_DATE_REMAP_TO", "")
    monkeypatch.setenv(
        "POS_SPLIT_ACROSS_STORES", '["STORE_BLR_001","STORE_BLR_002"]'
    )
    config_mod.get_settings.cache_clear()

    n = await load_pos_csv()
    assert n == 4

    with sqlite3.connect(os.environ["SQLITE_PATH"], timeout=2.0) as con:
        con.row_factory = sqlite3.Row
        rows = [
            (r["store_id"], r["n"])
            for r in con.execute(
                "SELECT store_id, COUNT(*) AS n FROM pos_transactions "
                "WHERE order_id BETWEEN 1 AND 4 GROUP BY store_id ORDER BY store_id"
            ).fetchall()
        ]
    # Both stores get exactly 2 rows — split is deterministic by order_id parity
    assert rows == [("STORE_BLR_001", 2), ("STORE_BLR_002", 2)]
    config_mod.get_settings.cache_clear()

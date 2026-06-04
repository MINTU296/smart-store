# PROMPT: "Generate a pytest conftest.py for a FastAPI app whose lifespan sets up
#   aiosqlite + redis. The tests should run against a temp file SQLite (not in-memory)
#   so the WAL pragma works, with a fakeredis client patched into the app's RedisClient
#   wrapper. Provide a TestClient fixture that runs the app's lifespan."
# CHANGES MADE:
#   - Switched the proposed `redis.from_url` monkeypatch to also stamp RedisClient._ok = True
#     so the ingestion side-effects path runs (the original suggestion only swapped the client
#     and the publish was silently skipped).
#   - Added a per-test cleanup that wipes the events table — without it, idempotency
#     and metrics tests leaked state into each other.
#   - Pinned the pytest_asyncio mode in pyproject (not here) and removed the redundant
#     `pytestmark = pytest.mark.asyncio` lines that the AI tried to add.
#   - M-7: replaced asyncio.new_event_loop() teardown with a synchronous
#     sqlite3 wipe. The aiosqlite connection is bound to the TestClient's
#     loop; touching it from a fresh loop on Python 3.12+ raises
#     "Future attached to a different loop". sqlite3 over the same DB file
#     is safe — WAL mode lets a sync writer commit while the async conn is
#     idle (the TestClient has already exited its lifespan by this point).
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set env vars BEFORE importing app so Settings picks them up
_TEMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TEMP_DB.close()
os.environ.setdefault("SQLITE_PATH", _TEMP_DB.name)
os.environ.setdefault("REDIS_URL", "redis://invalid-do-not-use:6379/0")
os.environ.setdefault("POS_CSV_PATH", str(ROOT / "tests" / "fixtures_pos.csv"))


@pytest.fixture(autouse=True)
def _patch_redis(monkeypatch):
    """Force the app's RedisClient to use fakeredis."""
    from app import db as db_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_open(cls_or_self, url=None):
        db_mod.RedisClient._client = fake
        db_mod.RedisClient._ok = True
        return db_mod.RedisClient()

    monkeypatch.setattr(db_mod.RedisClient, "open", classmethod(fake_open))
    yield


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c

    # Sync wipe over the same DB file. The TestClient's lifespan has exited by
    # the time we reach this point; the aiosqlite connection is closed and
    # WAL mode lets a fresh sqlite3 writer commit safely.
    db_path = os.environ["SQLITE_PATH"]
    if pathlib.Path(db_path).exists():
        try:
            con = sqlite3.connect(db_path, timeout=2.0)
            try:
                con.execute("DELETE FROM events")
                con.execute("DELETE FROM pos_transactions")
                con.commit()
            finally:
                con.close()
        except sqlite3.OperationalError:
            # Tables may not exist yet on the very first test if startup errored.
            pass


def _pos_fixture():
    p = ROOT / "tests" / "fixtures_pos.csv"
    if not p.exists():
        p.write_text(
            "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        )


_pos_fixture()

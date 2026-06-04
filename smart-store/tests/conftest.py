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
from __future__ import annotations

import os
import pathlib
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
        # cleanup events between tests so independence holds
        from app.db import Database

        if Database._instance and Database._instance._conn:
            try:
                import asyncio

                async def _wipe():
                    async with Database.instance().cursor() as cur:
                        await cur.execute("DELETE FROM events")
                        await cur.execute("DELETE FROM pos_transactions")

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(_wipe())
                finally:
                    loop.close()
            except Exception:
                pass


def _pos_fixture():
    p = ROOT / "tests" / "fixtures_pos.csv"
    if not p.exists():
        p.write_text(
            "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
        )


_pos_fixture()

"""Build + ship events to the API.

Events are deterministic by event_id (uuid5 of the natural key) so re-runs are
idempotent. Batches of up to PIPELINE_BATCH_SIZE are POSTed to /events/ingest;
HTTP errors are retried twice with backoff, then logged and skipped — we never
let a network glitch poison the pipeline.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Iterable

import httpx

from .config import CONFIG

log = logging.getLogger("pipeline.emit")

# Stable namespace for uuid5 — identical across runs of the pipeline.
NAMESPACE = uuid.UUID("a8f3a47e-2bd2-4c3d-9c12-7b1e3e0a6b6f")


def make_event_id(store_id: str, camera_id: str, visitor_id: str, event_type: str, ts_iso: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{store_id}|{camera_id}|{visitor_id}|{event_type}|{ts_iso}"))


def build_event(
    *,
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    ts: datetime,
    confidence: float,
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    session_seq: int | None = None,
    group_size: int | None = None,
) -> dict:
    ts_iso = ts.isoformat().replace("+00:00", "Z")
    if not ts_iso.endswith("Z"):
        ts_iso = ts_iso + "Z"
    return {
        "event_id": make_event_id(store_id, camera_id, visitor_id, event_type, ts_iso),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts_iso,
        "zone_id": zone_id,
        "dwell_ms": int(dwell_ms),
        "is_staff": bool(is_staff),
        "confidence": round(float(confidence), 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
            "group_size": group_size,
        },
    }


class EventEmitter:
    def __init__(self, api_base: str | None = None, batch_size: int | None = None):
        self.api_base = (api_base or CONFIG.api_base).rstrip("/")
        self.batch_size = batch_size or CONFIG.batch_size
        self._buffer: list[dict] = []
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0)
        return self._client

    def add(self, event: dict) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        payload = {"events": self._buffer}
        for attempt in range(3):
            try:
                r = self._http().post(f"{self.api_base}/events/ingest", json=payload)
                if r.status_code == 200:
                    body = r.json()
                    log.info(
                        "emit.flush size=%d accepted=%s duplicates=%s rejected=%s",
                        len(self._buffer),
                        body.get("accepted"),
                        body.get("duplicates"),
                        body.get("rejected"),
                    )
                    self._buffer.clear()
                    return
                log.warning(
                    "emit.flush_non200 attempt=%d status=%d body=%s",
                    attempt, r.status_code, r.text[:200],
                )
            except Exception as e:  # noqa: BLE001
                log.warning("emit.flush_err attempt=%d err=%s", attempt, e)
            time.sleep(0.5 * (attempt + 1))
        log.error("emit.flush_dropped size=%d", len(self._buffer))
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._client:
            self._client.close()


def write_jsonl(events: Iterable[dict], path: str) -> int:
    """Local sink for offline replay. Useful when API is not reachable."""
    n = 0
    with open(path, "w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
            n += 1
    return n

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Loaded once via lru_cache.

    All values are overridable through env vars; defaults match docker-compose.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_log_level: str = "INFO"
    api_confidence_floor: float = 0.4

    sqlite_path: str = "./store_intelligence.db"
    redis_url: str = "redis://localhost:6379/0"

    pos_csv_path: str = "/raw-data/POS - sample transactionsb1e826f.csv"
    pos_correlation_window_sec: int = 300

    # CSV-to-canonical store_id mapping. The supplied POS CSV uses opaque
    # codes (e.g. ST1008) instead of the canonical STORE_BLR_* IDs the
    # events table and store_layouts use. This map is consulted at
    # CSV-load time so the pos_transactions rows correlate with visitor
    # sessions. JSON-encoded for env friendliness:
    #   POS_STORE_ID_MAP='{"ST1008":"STORE_BLR_001"}'
    pos_store_id_map: str = "{}"

    # Optional ISO date (YYYY-MM-DD). When set, every CSV row's parsed
    # datetime has its date component replaced by this value, keeping
    # HH:MM:SS. Used to align the supplied sample-day with the pipeline's
    # clip-start day so demos surface non-zero conversion. Empty disables
    # the remap (production behaviour).
    pos_date_remap_to: str = ""

    # Demo-only: round-robin distribute CSV rows across these store ids by
    # order_id parity (modulo). Used because the supplied sample CSV only
    # has one store-id (ST1008) but the demo has two stores; without this,
    # Store 2 always shows Purchase=0. JSON-array of canonical store ids:
    #   POS_SPLIT_ACROSS_STORES=["STORE_BLR_001","STORE_BLR_002"]
    # Empty disables the split (production behaviour).
    pos_split_across_stores: str = "[]"

    queue_spike_depth: int = 5
    queue_spike_p95_window_min: int = 60
    queue_spike_p95_min_samples: int = 5
    dead_zone_minutes: int = 30
    stale_feed_minutes: int = 10

    layout_dir: str = Field(default="./store_layouts")

    @property
    def layout_path(self) -> Path:
        return Path(self.layout_dir)


def parse_store_id_map(raw: str) -> dict[str, str]:
    """Parse the POS_STORE_ID_MAP env var (JSON object string) into a dict.

    Returns an empty dict on missing/malformed input — the loader treats
    that as "no translation", which is the correct production default.
    """
    if not raw or raw == "{}":
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


def parse_split_stores(raw: str) -> list[str]:
    """Parse POS_SPLIT_ACROSS_STORES (JSON array of store ids).

    Returns an empty list on missing/malformed input — the loader treats
    that as "no split", which is the correct production default.
    """
    if not raw or raw == "[]":
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(x) for x in decoded if x]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

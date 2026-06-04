from __future__ import annotations

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

    queue_spike_depth: int = 5
    queue_spike_p95_window_min: int = 60
    queue_spike_p95_min_samples: int = 5
    dead_zone_minutes: int = 30
    stale_feed_minutes: int = 10

    layout_dir: str = Field(default="./store_layouts")

    @property
    def layout_path(self) -> Path:
        return Path(self.layout_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

"""Pipeline configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PipelineConfig:
    api_base: str = os.getenv("PIPELINE_API_BASE", "http://localhost:8000")
    batch_size: int = int(os.getenv("PIPELINE_BATCH_SIZE", "200"))
    # 8 fps lands inside the 5–10 fps retail-analytics envelope; halves GPU
    # cost vs 15 fps without losing tracking quality on walking shoppers.
    fps: int = int(os.getenv("PIPELINE_FPS", "8"))
    reid_threshold: float = float(os.getenv("PIPELINE_REID_THRESHOLD", "0.75"))
    reentry_window_sec: int = int(os.getenv("PIPELINE_REENTRY_WINDOW_SEC", "1800"))
    dwell_emit_sec: int = int(os.getenv("PIPELINE_DWELL_EMIT_SEC", "30"))
    confidence_floor: float = float(os.getenv("PIPELINE_CONFIDENCE_FLOOR", "0.25"))

    # Default frame_stride for floor / billing cameras. Entry cameras always
    # use entry_stride=1 — line crossings can complete in <0.5s, missing one
    # is a counted-customer error.
    frame_stride: int = int(os.getenv("PIPELINE_FRAME_STRIDE", "3"))
    entry_stride: int = int(os.getenv("PIPELINE_ENTRY_STRIDE", "1"))

    yolo_model: str = os.getenv("PIPELINE_YOLO", "yolo11n.pt")  # YOLOv11 nano
    person_class_id: int = 0  # COCO 'person' index in YOLO

    # Synthetic clip start time when timestamps are missing from the file
    default_clip_start_iso: str = os.getenv(
        "PIPELINE_CLIP_START", "2026-03-08T18:00:00Z"
    )


CONFIG = PipelineConfig()

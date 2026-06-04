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
    # Maximum lifetime of an identity in the Re-ID index, regardless of
    # whether it has been re-observed recently. Without this, a long-stayer
    # (visitor who entered hours ago and is still being tracked) keeps
    # their embedding eligible for matches forever, so a similarly-dressed
    # arrival much later can falsely register as a REENTRY of that earlier
    # session. 45 minutes is well above a typical retail visit but well
    # below the kinds of multi-hour sessions where the embedding's
    # lighting drift would degrade matches anyway.
    reid_max_lifetime_sec: int = int(os.getenv("PIPELINE_REID_MAX_LIFETIME_SEC", "2700"))
    dwell_emit_sec: int = int(os.getenv("PIPELINE_DWELL_EMIT_SEC", "30"))
    # YOLO inference floor — kept low so low-confidence detections are still
    # surfaced to the state machines (spec: "low-confidence detections must
    # be FLAGGED, not silently dropped or falsely elevated"). The detection
    # state machines emit events with the actual `det.confidence` value, so
    # a 0.12-conf occluded detection produces a 0.12-conf event downstream
    # instead of being filtered out at inference time.
    confidence_floor: float = float(os.getenv("PIPELINE_CONFIDENCE_FLOOR", "0.10"))

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

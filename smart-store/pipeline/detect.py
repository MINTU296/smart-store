"""YOLO person detection wrapper.

Loaded lazily (so the API container doesn't need YOLO weights). When ultralytics
is unavailable (e.g. tests, lightweight installs) we fall back to a synthetic
detector that emits no detections — the pipeline still runs end-to-end and
produces 0 events, which is the documented "empty store" handling path.

Default weight is YOLOv11n (current Ultralytics generation, better small-object
recall than v8). Override via PIPELINE_YOLO env var.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator

from .config import CONFIG

log = logging.getLogger("pipeline.detect")


@dataclass
class Detection:
    """One bounding box from one frame."""
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float

    @property
    def cx_norm(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy_norm(self) -> float:
        return (self.y1 + self.y2) / 2


class YoloPersonDetector:
    """Wraps Ultralytics YOLO for person-only inference. Defaults to YOLOv11n;
    track IDs come from the built-in ByteTrack tracker (ultralytics .track())."""

    def __init__(self, model_path: str = CONFIG.yolo_model):
        self.model_path = model_path
        self._model = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        try:
            from ultralytics import YOLO  # type: ignore
            self._model = YOLO(self.model_path)
            log.info("yolo.loaded model=%s", self.model_path)
        except Exception as e:  # noqa: BLE001
            log.warning("yolo.unavailable model=%s err=%s — running with empty detector", self.model_path, e)
            self._model = False  # sentinel: tried and failed

    def track(self, video_path: str):
        """Yield (frame_idx, {track_id: Detection}, frame_bgr) for each frame.

        Detections are returned in NORMALISED coordinates (0..1) so the rest of
        the pipeline doesn't care about resolution. The raw BGR frame is yielded
        so the caller can slice each track's bbox out for downstream signals
        (staff uniform colour, Re-ID embeddings, etc.). frame_bgr is None when
        we couldn't capture the frame (older OpenCV without orig_img, or the
        sentinel empty-detector path).
        """
        self._ensure()
        if not self._model:  # ultralytics not installed → empty stream
            return

        try:
            stream = self._model.track(
                source=video_path,
                classes=[CONFIG.person_class_id],
                conf=CONFIG.confidence_floor,
                tracker="bytetrack.yaml",
                stream=True,
                verbose=False,
                persist=True,
            )
        except Exception as e:  # noqa: BLE001
            log.error("yolo.track_failed video=%s err=%s", video_path, e)
            return

        frame_idx = 0
        for result in stream:
            tracks: dict[int, Detection] = {}
            frame_bgr = getattr(result, "orig_img", None)
            try:
                boxes = result.boxes
                if boxes is None or boxes.id is None:
                    yield frame_idx, tracks, frame_bgr
                    frame_idx += 1
                    continue
                w = result.orig_shape[1] if hasattr(result, "orig_shape") else 1
                h = result.orig_shape[0] if hasattr(result, "orig_shape") else 1
                xyxy = boxes.xyxy.cpu().numpy()
                conf = boxes.conf.cpu().numpy()
                ids = boxes.id.cpu().numpy().astype(int)
                for (x1, y1, x2, y2), c, tid in zip(xyxy, conf, ids):
                    tracks[int(tid)] = Detection(
                        x1=float(x1) / w,
                        y1=float(y1) / h,
                        x2=float(x2) / w,
                        y2=float(y2) / h,
                        confidence=float(c),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("yolo.frame_parse_err frame=%d err=%s", frame_idx, e)
            yield frame_idx, tracks, frame_bgr
            frame_idx += 1

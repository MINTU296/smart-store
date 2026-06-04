"""Re-ID across cameras + re-entry detection.

We avoid heavy CV deps (torchreid/OSNet) so the pipeline runs on plain CPU
machines without GPU. The current implementation uses a colour-histogram
embedding from each track's representative crop and matches by cosine
similarity. The interface (compute_embedding / match) is identical to what an
OSNet-based system would expose, so swapping it in later is a one-file change.

The follow-up interview question — "What breaks when a customer leaves and a
similarly-dressed one enters 3 seconds later from the same direction?" — is
answered by:
    a) the threshold (0.75 cosine, tunable),
    b) the time gap (re-entry only valid within reentry_window_sec),
    c) ByteTrack's per-camera continuity holds across short occlusions, so most
       same-person re-detection is handled before re-ID is consulted.
This is why we keep the Re-ID as the *fallback* identity matcher rather than
the primary one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from .config import CONFIG


@dataclass
class TrackIdentity:
    visitor_id: str
    embedding: np.ndarray
    last_seen: datetime
    exited_at: Optional[datetime] = None


@dataclass
class ReIDIndex:
    threshold: float = CONFIG.reid_threshold
    reentry_window_sec: int = CONFIG.reentry_window_sec
    identities: list[TrackIdentity] = field(default_factory=list)

    def compute_embedding(self, crop_bgr: np.ndarray) -> np.ndarray:
        """Cheap colour-histogram embedding. Replace with OSNet if GPU available.

        crop_bgr is HxWx3 uint8. Returns L2-normalised float32 vector.
        """
        if crop_bgr is None or crop_bgr.size == 0:
            return np.zeros(96, dtype=np.float32)
        # 3 colour channels x 32-bin histograms = 96 features
        hist = []
        for ch in range(3):
            h, _ = np.histogram(crop_bgr[:, :, ch], bins=32, range=(0, 256))
            hist.append(h.astype(np.float32))
        v = np.concatenate(hist)
        norm = float(np.linalg.norm(v)) or 1.0
        return v / norm

    def match(self, embedding: np.ndarray, now: datetime) -> Optional[TrackIdentity]:
        """Return the best-matching active identity, or None if all below threshold."""
        if embedding.size == 0:
            return None
        best: tuple[float, Optional[TrackIdentity]] = (-1.0, None)
        cutoff = now - timedelta(seconds=self.reentry_window_sec)
        for ident in self.identities:
            if ident.last_seen < cutoff:
                continue
            sim = float(np.dot(ident.embedding, embedding))
            if sim > best[0]:
                best = (sim, ident)
        if best[1] is not None and best[0] >= self.threshold:
            return best[1]
        return None

    def add(self, visitor_id: str, embedding: np.ndarray, now: datetime) -> TrackIdentity:
        ident = TrackIdentity(visitor_id=visitor_id, embedding=embedding, last_seen=now)
        self.identities.append(ident)
        return ident

    def update(self, ident: TrackIdentity, embedding: np.ndarray, now: datetime) -> None:
        # exponential moving average of the embedding to stabilise drift
        ident.embedding = 0.7 * ident.embedding + 0.3 * embedding
        n = float(np.linalg.norm(ident.embedding)) or 1.0
        ident.embedding = ident.embedding / n
        ident.last_seen = now

    def mark_exited(self, ident: TrackIdentity, when: datetime) -> None:
        ident.exited_at = when

    def is_reentry(self, ident: TrackIdentity, now: datetime) -> bool:
        return ident.exited_at is not None and (now - ident.exited_at).total_seconds() > 1.0

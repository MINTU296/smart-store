"""Debug helper: probe the staff classifier against real video frames.

Walks one frame from each clip, runs YOLO, extracts bbox crops, and prints the
shirt/trouser HSV ratios for each detection. Useful for sanity-checking the
SHIRT_RANGES + BLACK_TROUSER_RANGE thresholds against real data.

Usage (inside the pipeline container):
    python scripts/debug_staff.py --store STORE_BLR_001 --clip-dir "/raw-data/Store 1"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2  # type: ignore
import numpy as np  # type: ignore

from pipeline.detect import YoloPersonDetector
from pipeline.staff import SHIRT_RANGES, BLACK_TROUSER_RANGE
from pipeline.zones import load_layout


def shirt_trouser_ratios(crop_bgr, store_id: str):
    h, w = crop_bgr.shape[:2]
    if min(h, w) < 8:
        return None
    shirt = crop_bgr[: max(1, h // 3), :, :]
    trousers = crop_bgr[h // 3 :, :, :]
    shirt_hsv = cv2.cvtColor(shirt, cv2.COLOR_BGR2HSV)
    trousers_hsv = cv2.cvtColor(trousers, cv2.COLOR_BGR2HSV)

    ranges = SHIRT_RANGES.get(store_id, SHIRT_RANGES["STORE_BLR_001"])
    shirt_mask = None
    for lo, hi in ranges:
        m = cv2.inRange(shirt_hsv, np.array(lo, dtype=np.uint8), np.array(hi, dtype=np.uint8))
        shirt_mask = m if shirt_mask is None else cv2.bitwise_or(shirt_mask, m)
    shirt_ratio = float(shirt_mask.sum()) / float(255 * shirt_mask.size)

    trouser_mask = cv2.inRange(
        trousers_hsv,
        np.array(BLACK_TROUSER_RANGE[0], dtype=np.uint8),
        np.array(BLACK_TROUSER_RANGE[1], dtype=np.uint8),
    )
    trouser_ratio = float(trouser_mask.sum()) / float(255 * trouser_mask.size)

    # Also report the dominant shirt HSV mean for context.
    shirt_mean = shirt_hsv.reshape(-1, 3).mean(axis=0)
    return shirt_ratio, trouser_ratio, shirt_mean


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    p.add_argument("--clip-dir", required=True)
    p.add_argument("--frames-per-clip", type=int, default=20)
    args = p.parse_args()

    layout_path = f"store_layouts/{args.store}.json"
    if not Path(layout_path).exists():
        layout_path = str(Path(__file__).resolve().parent.parent / "store_layouts" / f"{args.store}.json")
    layout = load_layout(layout_path)

    print(f"=== Debugging staff classifier for {args.store} ===")
    print(f"SHIRT_RANGES: {SHIRT_RANGES.get(args.store)}")
    print(f"BLACK_TROUSER_RANGE: {BLACK_TROUSER_RANGE}")
    print()

    clip_dir = Path(args.clip_dir)
    for clip in sorted(clip_dir.iterdir()):
        if clip.suffix.lower() not in (".mp4", ".mov", ".avi"):
            continue
        cam_id = layout.clip_camera_map.get(clip.name)
        if cam_id is None:
            continue
        print(f"--- Clip: {clip.name} (cam={cam_id}) ---")

        detector = YoloPersonDetector()
        sampled = 0
        for frame_idx, dets, frame_bgr in detector.track(str(clip)):
            if frame_idx % 50 != 0:
                continue
            if frame_bgr is None or not dets:
                continue
            fh, fw = frame_bgr.shape[:2]
            for tid, det in dets.items():
                x1 = max(0, int(det.x1 * fw))
                y1 = max(0, int(det.y1 * fh))
                x2 = min(fw, int(det.x2 * fw))
                y2 = min(fh, int(det.y2 * fh))
                if (x2 - x1) < 8 or (y2 - y1) < 8:
                    continue
                crop = frame_bgr[y1:y2, x1:x2, :]
                ratios = shirt_trouser_ratios(crop, args.store)
                if ratios is None:
                    continue
                shirt_r, trouser_r, mean_hsv = ratios
                hh, ss, vv = mean_hsv
                print(
                    f"  frame={frame_idx:5d} tid={tid:3d} bbox=({x1},{y1})-({x2},{y2}) "
                    f"shirt_ratio={shirt_r:.2f} trouser_ratio={trouser_r:.2f} "
                    f"shirt_mean H={hh:.0f} S={ss:.0f} V={vv:.0f}"
                )
            sampled += 1
            if sampled >= args.frames_per_clip:
                break
        print()


if __name__ == "__main__":
    main()

"""Zone polygon utilities + ZONE_ENTER/EXIT/DWELL state machine.

Polygons are stored normalised (0..1) in store_layouts/*.json. We convert them
to image coordinates lazily based on each frame's (W, H).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class Zone:
    zone_id: str
    polygon_norm: list[tuple[float, float]]


@dataclass
class CameraLayout:
    name: str
    role: str  # "entry" | "floor" | "billing"
    zones: list[Zone] = field(default_factory=list)
    entry_line: dict | None = None
    queue_polygon: list[tuple[float, float]] | None = None
    overlap_with: list[str] = field(default_factory=list)


@dataclass
class StoreLayout:
    store_id: str
    cameras: dict[str, CameraLayout]
    clip_camera_map: dict[str, str]


def load_layout(path: str | Path) -> StoreLayout:
    p = Path(path)
    raw = json.loads(p.read_text())
    cams: dict[str, CameraLayout] = {}
    for cam_name, cam in raw.get("cameras", {}).items():
        zones = [
            Zone(zone_id=z["zone_id"], polygon_norm=[tuple(pt) for pt in z["polygon"]])
            for z in cam.get("zones", [])
        ]
        queue_poly = (
            [tuple(pt) for pt in cam["queue_polygon"]] if cam.get("queue_polygon") else None
        )
        cams[cam_name] = CameraLayout(
            name=cam_name,
            role=cam.get("role", "floor"),
            zones=zones,
            entry_line=cam.get("entry_line"),
            queue_polygon=queue_poly,
            overlap_with=cam.get("overlap_with", []),
        )
    return StoreLayout(
        store_id=raw["store_id"],
        cameras=cams,
        clip_camera_map=raw.get("clip_camera_map", {}),
    )


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon. Polygon is closed implicitly."""
    x, y = point
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # cross-product sign test
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def find_zone(point_norm: tuple[float, float], zones: Iterable[Zone]) -> str | None:
    """Return the first zone_id whose polygon contains the (normalised) point."""
    for z in zones:
        if point_in_polygon(point_norm, z.polygon_norm):
            return z.zone_id
    return None

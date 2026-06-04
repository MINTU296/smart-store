# PROMPT: "Write tests for the StaffClassifier uniform-colour heuristic. Verify that
#   a synthetic all-black crop classifies as staff under STORE_BLR_001's palette but as
#   customer under STORE_BLR_002's (where staff wear pink shirts), and that the
#   behaviour fallback (long floor dwell, no billing visit) catches staff even with no
#   crop available."
# CHANGES MADE:
#   - The first cut of the test relied on cv2 being installed; the project's test
#     environment doesn't always have it. I gated the colour assertions behind a
#     skip-if-cv2-missing import so the suite still runs in lightweight setups.
#   - Added an explicit cache-hit test — a second classify() call must not re-run
#     the colour analysis.
from __future__ import annotations

import importlib

import pytest

from pipeline.staff import StaffClassifier

cv2 = pytest.importorskip("cv2", reason="cv2 not installed; uniform tests require OpenCV")
np = pytest.importorskip("numpy")


def _crop(rgb_top: tuple[int, int, int], rgb_bottom: tuple[int, int, int], h: int = 60, w: int = 30) -> "np.ndarray":
    """Build a synthetic crop: top third = rgb_top, lower two thirds = rgb_bottom."""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[: h // 3, :] = rgb_top[::-1]   # BGR
    arr[h // 3 :, :] = rgb_bottom[::-1]
    return arr


def test_store1_all_black_is_staff_after_persistence():
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_001")
    crop = _crop((10, 10, 10), (15, 15, 15))  # near-black top + bottom
    # Classifier requires `min_consecutive_matches` consecutive uniform hits
    # before flipping is_staff=True (a single dark-clothed customer frame
    # must NOT be enough). After the threshold of consecutive matches, the
    # verdict locks in.
    for _ in range(sc.min_consecutive_matches - 1):
        assert sc.classify("V_s1_a", crop_bgr=crop) is False
    assert sc.classify("V_s1_a", crop_bgr=crop) is True


def test_store2_pink_shirt_black_pants_is_staff_after_persistence():
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_002")
    crop = _crop((230, 60, 130), (10, 10, 10))  # pink top, black bottom
    for _ in range(sc.min_consecutive_matches - 1):
        assert sc.classify("V_s2_a", crop_bgr=crop) is False
    assert sc.classify("V_s2_a", crop_bgr=crop) is True


def test_persistence_resets_on_non_match():
    """A single non-match clears the streak — a customer who momentarily
    walks past in dark clothes shouldn't accumulate a fake staff verdict."""
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_001")
    black = _crop((10, 10, 10), (15, 15, 15))
    bright = _crop((250, 250, 250), (250, 250, 250))  # white top — clear non-match
    sc.classify("V_reset", crop_bgr=black)
    sc.classify("V_reset", crop_bgr=black)
    sc.classify("V_reset", crop_bgr=bright)  # streak resets to 0
    # Need full N consecutive matches again, not just 1
    for _ in range(sc.min_consecutive_matches - 1):
        assert sc.classify("V_reset", crop_bgr=black) is False
    assert sc.classify("V_reset", crop_bgr=black) is True


def test_store2_all_black_is_NOT_staff():
    """In Store 2, all-black is a customer (only pink-shirt qualifies as uniform)."""
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_002")
    crop = _crop((10, 10, 10), (10, 10, 10))
    # not staff via uniform; behaviour fallback also negative
    assert sc.classify("V_s2_b", crop_bgr=crop, dwell_seconds_in_floor=60, visited_billing=True) is False


def test_behaviour_fallback_catches_staff_without_crop():
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_001")
    # No crop, but >20 min on the floor with no billing visit → treat as staff.
    assert sc.classify("V_b_1", crop_bgr=None, dwell_seconds_in_floor=21 * 60, visited_billing=False) is True


def test_classify_is_cached_after_persistence(monkeypatch):
    """Once min_consecutive_matches True samples have flipped the cache to
    True, a subsequent classify() must be a pure cache read — no further
    HSV work."""
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_001")
    calls = {"n": 0}

    real = sc._uniform_match

    def counted(crop_bgr):
        calls["n"] += 1
        return real(crop_bgr)

    monkeypatch.setattr(sc, "_uniform_match", counted)
    crop = _crop((10, 10, 10), (10, 10, 10))
    # Drive the classifier through the persistence threshold.
    for _ in range(sc.min_consecutive_matches):
        sc.classify("V_cache", crop_bgr=crop)
    n_before = calls["n"]
    # Cache hit — no more _uniform_match calls.
    sc.classify("V_cache", crop_bgr=crop)
    sc.classify("V_cache", crop_bgr=crop)
    assert calls["n"] == n_before


def test_vlm_dry_run_writes_audit_for_ambiguous_crops(tmp_path, monkeypatch):
    """Dry-run mode must log the prompt + crop_hash for ambiguous crops, capped
    at 3 entries. The crop must be ambiguous (uniform_match returns None) for
    _call_vlm to fire — we force that by stubbing _uniform_match."""
    audit_path = tmp_path / "vlm_audit.jsonl"
    sc = StaffClassifier()
    sc.set_store("STORE_BLR_002")
    sc.vlm_dry_run = True
    sc.vlm_audit_path = str(audit_path)

    # Force the uniform branch to be inconclusive so _call_vlm runs.
    monkeypatch.setattr(sc, "_uniform_match", lambda crop_bgr: None)

    crop = _crop((10, 10, 10), (10, 10, 10))
    for i in range(5):
        sc.classify(f"V_dry_{i}", crop_bgr=crop)

    assert audit_path.exists(), "vlm_audit.jsonl should be written in dry-run mode"
    lines = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    # Capped at 3 audited calls per run
    assert len(lines) == 3, f"expected 3 audit entries, got {len(lines)}"
    import json as _json

    for ln in lines:
        entry = _json.loads(ln)
        assert entry["store_id"] == "STORE_BLR_002"
        assert "pink shirts and black trousers" in entry["uniform_desc"]
        assert "Apex Retail" in entry["prompt"]
        assert entry["crop_hash"]  # non-empty hash
        assert "would_call_provider" in entry

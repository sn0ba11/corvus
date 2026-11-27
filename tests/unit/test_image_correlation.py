from pathlib import Path

import pytest
from PIL import Image

from app.services.image_correlation import (
    ImageCorrelationError,
    ImageFingerprint,
    correlate_fingerprints,
    hamming_distance,
    perceptual_dhash,
)


def save_gradient(path: Path, *, reverse: bool = False) -> None:
    image = Image.new("L", (16, 16))
    values = []
    for _row in range(16):
        row = list(range(16))
        values.extend(reversed(row) if reverse else row)
    image.putdata(values)
    image.save(path)


def test_perceptual_hash_is_stable_and_distinguishes_direction(tmp_path: Path) -> None:
    left = tmp_path / "left.png"
    resized = tmp_path / "resized.png"
    right = tmp_path / "right.png"
    save_gradient(left)
    with Image.open(left) as image:
        image.resize((64, 64)).save(resized)
    save_gradient(right, reverse=True)

    assert perceptual_dhash(left) == perceptual_dhash(resized)
    assert hamming_distance(perceptual_dhash(left), perceptual_dhash(right)) == 64


def test_exact_and_perceptual_matches_are_classified() -> None:
    fingerprints = {
        "a": ImageFingerprint("same", "0000000000000000"),
        "b": ImageFingerprint("same", "ffffffffffffffff"),
        "c": ImageFingerprint("different", "0000000000000001"),
        "d": ImageFingerprint("other", "ffffffffffffffff"),
    }
    matches = correlate_fingerprints(fingerprints, threshold=1)

    assert [(item.left_id, item.right_id, item.match_type, item.distance) for item in matches] == [
        ("a", "b", "exact", 0),
        ("a", "c", "perceptual", 1),
        ("b", "d", "perceptual", 0),
    ]


def test_rejects_invalid_hashes_and_thresholds() -> None:
    with pytest.raises(ImageCorrelationError, match="incompatible"):
        hamming_distance("00", "0000")
    with pytest.raises(ImageCorrelationError, match="invalid"):
        hamming_distance("zz", "00")
    with pytest.raises(ImageCorrelationError, match="between 0 and 64"):
        correlate_fingerprints({}, threshold=65)

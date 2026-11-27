from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image, UnidentifiedImageError


class ImageCorrelationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImageFingerprint:
    sha256: str
    dhash: str


@dataclass(frozen=True, slots=True)
class ImageMatch:
    left_id: str
    right_id: str
    match_type: str
    distance: int


def perceptual_dhash(path: Path, *, hash_size: int = 8) -> str:
    try:
        with Image.open(path) as image:
            grayscale = image.convert("L").resize((hash_size + 1, hash_size))
            pixels = list(cast(list[int], grayscale.get_flattened_data()))
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ImageCorrelationError("cached image could not be fingerprinted") from exc

    bits = 0
    width = hash_size + 1
    for row in range(hash_size):
        offset = row * width
        for column in range(hash_size):
            bits = (bits << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
    return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        raise ImageCorrelationError("perceptual hashes have incompatible lengths")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise ImageCorrelationError("perceptual hash is invalid") from exc


def correlate_fingerprints(
    fingerprints: dict[str, ImageFingerprint], *, threshold: int
) -> list[ImageMatch]:
    if not 0 <= threshold <= 64:
        raise ImageCorrelationError("perceptual threshold must be between 0 and 64")

    matches: list[ImageMatch] = []
    ids = sorted(fingerprints)
    for index, left_id in enumerate(ids):
        left = fingerprints[left_id]
        for right_id in ids[index + 1 :]:
            right = fingerprints[right_id]
            if left.sha256 == right.sha256:
                matches.append(ImageMatch(left_id, right_id, "exact", 0))
                continue
            distance = hamming_distance(left.dhash, right.dhash)
            if distance <= threshold:
                matches.append(ImageMatch(left_id, right_id, "perceptual", distance))
    return matches

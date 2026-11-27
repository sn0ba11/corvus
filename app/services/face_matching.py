from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2 as cv
import numpy as np
from numpy.typing import NDArray


class FaceMatchingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FaceEmbedding:
    vector: tuple[float, ...]
    detection_confidence: float
    bounding_box: tuple[int, int, int, int]
    faces_detected: int


@dataclass(frozen=True, slots=True)
class FaceMatch:
    left_id: str
    right_id: str
    similarity: float


class FaceMatcher:
    """CPU-only YuNet detection and SFace embedding extraction."""

    def __init__(
        self,
        detection_model: Path,
        recognition_model: Path,
        *,
        detection_threshold: float = 0.8,
    ) -> None:
        if not detection_model.is_file() or not recognition_model.is_file():
            raise FaceMatchingError("face matching models are unavailable")
        if not 0.0 < detection_threshold <= 1.0:
            raise FaceMatchingError("face detection threshold must be between 0 and 1")
        self.detection_threshold = detection_threshold
        try:
            self.detector: Any = cv.FaceDetectorYN.create(
                str(detection_model),
                "",
                (320, 320),
                detection_threshold,
                0.3,
                5000,
            )
            self.recognizer: Any = cv.FaceRecognizerSF.create(str(recognition_model), "")
        except cv.error as exc:
            raise FaceMatchingError("face matching models could not be loaded") from exc

    def embed(self, path: Path) -> FaceEmbedding:
        image = cv.imread(str(path), cv.IMREAD_COLOR)
        if image is None or image.ndim != 3:
            raise FaceMatchingError("profile image could not be decoded for face matching")
        height, width = image.shape[:2]
        if width < 20 or height < 20:
            raise FaceMatchingError("profile image is too small for face matching")

        try:
            self.detector.setInputSize((width, height))
            _, detected = self.detector.detect(image)
        except cv.error as exc:
            raise FaceMatchingError("face detection failed") from exc
        if detected is None or len(detected) == 0:
            raise FaceMatchingError("no face detected")

        faces: NDArray[np.float32] = np.asarray(detected, dtype=np.float32)
        primary = max(
            faces,
            key=lambda face: float(face[2] * face[3]) * max(float(face[-1]), 0.01),
        )
        try:
            aligned = self.recognizer.alignCrop(image, primary)
            feature = np.asarray(self.recognizer.feature(aligned), dtype=np.float32).reshape(-1)
        except cv.error as exc:
            raise FaceMatchingError("face alignment or embedding failed") from exc
        norm = float(np.linalg.norm(feature))
        if not np.isfinite(norm) or norm <= 0:
            raise FaceMatchingError("face embedding is invalid")
        feature /= norm

        x, y, box_width, box_height = (int(round(float(value))) for value in primary[:4])
        return FaceEmbedding(
            vector=tuple(float(value) for value in feature),
            detection_confidence=round(float(primary[-1]), 6),
            bounding_box=(x, y, box_width, box_height),
            faces_detected=int(len(faces)),
        )


def cosine_similarity(left: FaceEmbedding, right: FaceEmbedding) -> float:
    if len(left.vector) != len(right.vector) or not left.vector:
        raise FaceMatchingError("face embeddings have incompatible dimensions")
    similarity = float(np.dot(np.asarray(left.vector), np.asarray(right.vector)))
    return max(-1.0, min(1.0, similarity))


def correlate_faces(
    embeddings: dict[str, FaceEmbedding],
    *,
    query_ids: set[str],
    threshold: float,
    maximum_matches_per_query: int = 5,
) -> list[FaceMatch]:
    if not 0.0 <= threshold <= 1.0:
        raise FaceMatchingError("face similarity threshold must be between 0 and 1")
    if maximum_matches_per_query < 1:
        raise FaceMatchingError("maximum face matches must be positive")

    matches: list[FaceMatch] = []
    for left_id in sorted(query_ids & embeddings.keys()):
        ranked: list[FaceMatch] = []
        for right_id, right in embeddings.items():
            if right_id == left_id:
                continue
            similarity = cosine_similarity(embeddings[left_id], right)
            if similarity >= threshold:
                ranked.append(FaceMatch(left_id, right_id, similarity))
        ranked.sort(key=lambda match: (-match.similarity, match.right_id))
        matches.extend(ranked[:maximum_matches_per_query])
    return matches

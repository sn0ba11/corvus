from pathlib import Path

import pytest

from app.services.face_matching import (
    FaceEmbedding,
    FaceMatcher,
    FaceMatchingError,
    correlate_faces,
    cosine_similarity,
)


def embedding(*values: float) -> FaceEmbedding:
    return FaceEmbedding(
        vector=values,
        detection_confidence=0.95,
        bounding_box=(1, 2, 30, 40),
        faces_detected=1,
    )


def test_cosine_similarity_uses_normalised_embeddings() -> None:
    assert cosine_similarity(embedding(1.0, 0.0), embedding(1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity(embedding(1.0, 0.0), embedding(0.0, 1.0)) == pytest.approx(0.0)


def test_correlate_faces_returns_ranked_bounded_candidates_for_queries_only() -> None:
    embeddings = {
        "query": embedding(1.0, 0.0),
        "strong": embedding(0.99, 0.1),
        "medium": embedding(0.8, 0.6),
        "weak": embedding(0.0, 1.0),
    }

    matches = correlate_faces(
        embeddings,
        query_ids={"query"},
        threshold=0.5,
        maximum_matches_per_query=2,
    )

    assert [(match.left_id, match.right_id) for match in matches] == [
        ("query", "strong"),
        ("query", "medium"),
    ]
    assert matches[0].similarity > matches[1].similarity


def test_correlate_faces_rejects_invalid_threshold() -> None:
    with pytest.raises(FaceMatchingError, match="threshold"):
        correlate_faces({}, query_ids=set(), threshold=1.1)


def test_face_matcher_requires_pinned_models(tmp_path: Path) -> None:
    with pytest.raises(FaceMatchingError, match="unavailable"):
        FaceMatcher(tmp_path / "missing-yunet.onnx", tmp_path / "missing-sface.onnx")

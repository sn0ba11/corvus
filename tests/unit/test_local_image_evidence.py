from pathlib import Path

import pytest
from PIL import Image

from app.services.local_image_evidence import LocalImageEvidenceError, load_local_image


def test_loads_supported_collector_image_inside_raw_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    image_path = data_dir / "raw" / "case" / "run" / "profile_pic.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(image_path, format="JPEG")

    evidence = load_local_image(str(image_path), data_dir=data_dir, max_bytes=1024 * 1024)

    assert evidence.path == image_path.resolve()
    assert evidence.mime_type == "image/jpeg"
    assert evidence.byte_size == image_path.stat().st_size
    assert len(evidence.sha256) == 64
    assert evidence.metadata("https://cdn.example/avatar.jpg")["source"] == "collector_local_file"


def test_rejects_collector_image_outside_raw_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (8, 8)).save(outside, format="JPEG")

    with pytest.raises(LocalImageEvidenceError, match="outside raw evidence"):
        load_local_image(str(outside), data_dir=data_dir, max_bytes=1024 * 1024)


def test_rejects_non_image_payload(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = data_dir / "raw" / "case" / "run" / "profile_pic.jpg"
    path.parent.mkdir(parents=True)
    path.write_text("not an image", encoding="utf-8")

    with pytest.raises(LocalImageEvidenceError, match="supported image"):
        load_local_image(str(path), data_dir=data_dir, max_bytes=1024 * 1024)

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


class LocalImageEvidenceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LocalImageEvidence:
    path: Path
    mime_type: str
    byte_size: int
    sha256: str

    def metadata(self, source_url: str | None = None) -> dict[str, object]:
        return {
            "source_url": source_url,
            "source": "collector_local_file",
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "local_path": str(self.path),
        }


FORMAT_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def load_local_image(
    path_value: str,
    *,
    data_dir: Path,
    max_bytes: int,
) -> LocalImageEvidence:
    raw_root = (data_dir / "raw").resolve()
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = data_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LocalImageEvidenceError("collector image file is unavailable") from exc
    if not resolved.is_relative_to(raw_root):
        raise LocalImageEvidenceError("collector image path is outside raw evidence storage")
    if not resolved.is_file() or resolved.is_symlink():
        raise LocalImageEvidenceError("collector image path is not a regular evidence file")
    size = resolved.stat().st_size
    if size <= 0 or size > max_bytes:
        raise LocalImageEvidenceError("collector image exceeds the allowed size")
    payload = resolved.read_bytes()
    try:
        with Image.open(resolved) as image:
            image.verify()
            mime_type = FORMAT_MIME.get(str(image.format).upper())
    except (OSError, UnidentifiedImageError) as exc:
        raise LocalImageEvidenceError("collector image is not a supported image") from exc
    if mime_type is None:
        raise LocalImageEvidenceError("collector image format is unsupported")
    return LocalImageEvidence(
        path=resolved,
        mime_type=mime_type,
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )

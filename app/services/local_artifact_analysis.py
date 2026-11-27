from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring as safe_xml_fromstring  # type: ignore[import-untyped]
from PIL import ExifTags, Image

MAX_FILES = 100
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
SUPPORTED_SUFFIXES = {
    ".txt",
    ".html",
    ".htm",
    ".json",
    ".xml",
    ".csv",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".docx",
    ".xlsx",
    ".pptx",
}
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])")
COMPANY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'’.,\- ]{1,100}?\s(?i:Limited|Ltd\.?|PLC|LLC|Inc\.?|Incorporated|GmbH|S\.A\.?|S\.r\.l\.?|B\.V\.?))\b"
)
PDF_META_RE = re.compile(
    rb"/(Author|Creator|Producer|Title|Subject|CreationDate|ModDate)\s*\((.{0,1000}?)\)",
    re.DOTALL,
)
OFFICE_CORE_NAMES = {
    "title",
    "subject",
    "creator",
    "keywords",
    "description",
    "lastModifiedBy",
    "created",
    "modified",
    "category",
}


@dataclass(frozen=True, slots=True)
class ArtifactAnalysis:
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str
    metadata: dict[str, Any]
    emails: tuple[str, ...]
    companies: tuple[str, ...]


def _clean_text(value: Any, limit: int = 500) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).replace("\x00", " ").split())[:limit]
    return text or None


def _pdf_metadata(content: bytes) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for match in PDF_META_RE.finditer(content):
        key = match.group(1).decode("ascii", errors="ignore")
        value = _clean_text(match.group(2).decode("latin-1", errors="replace"), 1000)
        if value:
            metadata[key] = value
    return metadata


def _office_metadata(content: bytes) -> tuple[dict[str, str], str]:
    metadata: dict[str, str] = {}
    text_parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            remaining_bytes = MAX_FILE_BYTES
            if "docProps/core.xml" in names and archive.getinfo("docProps/core.xml").file_size <= remaining_bytes:
                core_xml = archive.read("docProps/core.xml")
                remaining_bytes -= len(core_xml)
                try:
                    root = safe_xml_fromstring(core_xml, forbid_dtd=True)
                except (ElementTree.ParseError, DefusedXmlException):
                    root = ElementTree.Element("blocked")
                for element in root.iter():
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in OFFICE_CORE_NAMES:
                        value = _clean_text(element.text, 1000)
                        if value:
                            metadata[local_name] = value
            candidate_names = [
                name
                for name in names
                if name == "word/document.xml"
                or name == "xl/sharedStrings.xml"
                or name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ][:100]
            for name in candidate_names:
                if archive.getinfo(name).file_size > remaining_bytes:
                    continue
                raw = archive.read(name)
                remaining_bytes -= len(raw)
                try:
                    root = safe_xml_fromstring(raw, forbid_dtd=True)
                except (ElementTree.ParseError, DefusedXmlException):
                    continue
                for element in root.iter():
                    local_name = element.tag.rsplit("}", 1)[-1]
                    if local_name in {"t", "v"} and element.text:
                        text_parts.append(element.text)
                        if sum(len(part) for part in text_parts) >= MAX_TEXT_CHARS:
                            break
    except (OSError, zipfile.BadZipFile):
        return {}, ""
    return metadata, " ".join(text_parts)[:MAX_TEXT_CHARS]


def _ratio(value: Any) -> float:
    try:
        numerator = float(value.numerator)
        denominator = float(value.denominator)
        return numerator / denominator if denominator else 0.0
    except AttributeError:
        return float(value)


def _gps_coordinate(values: Any, reference: Any) -> float | None:
    if not isinstance(values, tuple | list) or len(values) != 3:
        return None
    try:
        degrees, minutes, seconds = (_ratio(value) for value in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    coordinate = degrees + minutes / 60.0 + seconds / 3600.0
    if str(reference).upper() in {"S", "W"}:
        coordinate *= -1
    return round(coordinate, 7)


def _image_metadata(content: bytes) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        with Image.open(io.BytesIO(content)) as image:
            metadata["format"] = image.format
            metadata["width"] = image.width
            metadata["height"] = image.height
            exif = image.getexif()
            gps_info: dict[str, Any] | None = None
            for tag_id, value in exif.items():
                tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                if tag == "GPSInfo" and isinstance(value, dict):
                    gps_info = {
                        ExifTags.GPSTAGS.get(int(key), str(key)): item for key, item in value.items()
                    }
                    continue
                if tag in {
                    "Make",
                    "Model",
                    "Software",
                    "Artist",
                    "Copyright",
                    "DateTime",
                    "DateTimeOriginal",
                    "DateTimeDigitized",
                    "ImageDescription",
                }:
                    cleaned = _clean_text(value, 1000)
                    if cleaned:
                        metadata[tag] = cleaned
            if gps_info:
                latitude = _gps_coordinate(gps_info.get("GPSLatitude"), gps_info.get("GPSLatitudeRef"))
                longitude = _gps_coordinate(
                    gps_info.get("GPSLongitude"), gps_info.get("GPSLongitudeRef")
                )
                if latitude is not None and longitude is not None:
                    metadata["gps_latitude"] = latitude
                    metadata["gps_longitude"] = longitude
    except (OSError, ValueError):
        return {}
    return metadata


def _text_from_content(suffix: str, content: bytes) -> tuple[dict[str, Any], str]:
    if suffix in {".docx", ".xlsx", ".pptx"}:
        return _office_metadata(content)
    if suffix == ".pdf":
        return _pdf_metadata(content), content.decode("latin-1", errors="ignore")[:MAX_TEXT_CHARS]
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return _image_metadata(content), ""
    return {}, content.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]


def _media_type(suffix: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
        ".xml": "application/xml",
        ".csv": "text/csv",
    }.get(suffix, "text/plain")


def analyze_retained_artifacts(root: Path) -> list[ArtifactAnalysis]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    results: list[ArtifactAnalysis] = []
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        if len(results) >= MAX_FILES:
            break
        suffix = path.suffix.casefold()
        if suffix not in SUPPORTED_SUFFIXES:
            continue
        resolved = path.resolve()
        if resolved_root not in resolved.parents:
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            continue
        if size <= 0 or size > MAX_FILE_BYTES:
            continue
        try:
            content = resolved.read_bytes()
        except OSError:
            continue
        metadata, text = _text_from_content(suffix, content)
        emails = tuple(sorted(set(EMAIL_RE.findall(text)))[:100])
        companies = tuple(
            sorted({" ".join(match.split())[:200] for match in COMPANY_RE.findall(text)})[:100]
        )
        results.append(
            ArtifactAnalysis(
                relative_path=resolved.relative_to(resolved_root).as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                media_type=_media_type(suffix),
                metadata=metadata,
                emails=emails,
                companies=companies,
            )
        )
    return results

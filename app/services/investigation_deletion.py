"""Secure removal helpers for case-scoped investigation artifacts."""

import shutil
from pathlib import Path
from uuid import UUID


def canonical_investigation_id(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except ValueError as exc:
        raise ValueError("invalid investigation id") from exc
    if canonical != value.lower():
        raise ValueError("invalid investigation id")
    return canonical


def investigation_raw_directory(data_dir: Path, investigation_id: str) -> Path:
    canonical = canonical_investigation_id(investigation_id)
    root = (data_dir / "raw").resolve()
    destination = (root / canonical).resolve()
    if root not in destination.parents:
        raise ValueError("invalid investigation storage path")
    return destination


def remove_investigation_raw_artifacts(data_dir: Path, investigation_id: str) -> int:
    destination = investigation_raw_directory(data_dir, investigation_id)
    if not destination.exists():
        return 0
    removed_files = sum(1 for item in destination.rglob("*") if item.is_file())
    shutil.rmtree(destination)
    return removed_files

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.db import Investigation, ToolRun

MAX_RAW_JSON_BYTES = 5 * 1024 * 1024
MAX_RAW_JSON_ARTIFACTS_PER_RUN = 250


class RawJsonError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RawJsonArtifact:
    run_id: str
    tool_id: str
    display_name: str
    relative_path: str
    filename: str
    size_bytes: int
    category: str


def _run_root(data_dir: Path, investigation_id: str, run_id: str) -> Path:
    return (data_dir / "raw" / investigation_id / run_id).resolve()


def _bounded_json_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        base = Path(directory)
        for filename in filenames:
            if not filename.casefold().endswith(".json"):
                continue
            paths.append(base / filename)
            if len(paths) >= MAX_RAW_JSON_ARTIFACTS_PER_RUN:
                return paths
    return paths


def _category(path: Path) -> str:
    name = path.name.casefold()
    if name.startswith("report_") or name in {"profile.json", "results.json"}:
        return "collector result"
    if "enrichment" in name:
        return "public enrichment"
    return "raw JSON"


def list_raw_json_artifacts(data_dir: Path, investigation: Investigation) -> list[RawJsonArtifact]:
    artifacts: list[RawJsonArtifact] = []
    for run in sorted(investigation.runs, key=lambda item: (item.started_at is None, item.started_at)):
        root = _run_root(data_dir, investigation.id, run.id)
        if not root.is_dir():
            continue
        for path in _bounded_json_paths(root):
            if path.name.casefold() == "maigret-data.json":
                continue
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(root)
                size = resolved.stat().st_size
            except (OSError, ValueError):
                continue
            if not resolved.is_file() or size > MAX_RAW_JSON_BYTES:
                continue
            artifacts.append(
                RawJsonArtifact(
                    run_id=run.id,
                    tool_id=run.tool_id,
                    display_name=run.display_name,
                    relative_path=relative.as_posix(),
                    filename=relative.name,
                    size_bytes=size,
                    category=_category(relative),
                )
            )
    return artifacts


def _validated_artifact_path(
    data_dir: Path,
    investigation: Investigation,
    run: ToolRun,
    relative_path: str,
) -> Path:
    if run.investigation_id != investigation.id:
        raise RawJsonError("tool run does not belong to this investigation")
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RawJsonError("invalid raw JSON path")
    if relative.suffix.casefold() != ".json":
        raise RawJsonError("only JSON artifacts can be viewed")
    root = _run_root(data_dir, investigation.id, run.id)
    try:
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RawJsonError("raw JSON artifact was not found") from exc
    if not candidate.is_file():
        raise RawJsonError("raw JSON artifact was not found")
    if candidate.stat().st_size > MAX_RAW_JSON_BYTES:
        raise RawJsonError("raw JSON artifact exceeds the 5 MiB viewing limit")
    return candidate


def load_raw_json_artifact(
    data_dir: Path,
    investigation: Investigation,
    run: ToolRun,
    relative_path: str,
) -> tuple[bytes, str, Any]:
    path = _validated_artifact_path(data_dir, investigation, run, relative_path)
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawJsonError("artifact is not valid UTF-8 JSON") from exc
    pretty = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    return raw, pretty, payload

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.models.db import Investigation
from app.services.exports import investigation_dict

MAX_SELECTED_FINDINGS = 500
MAX_RAW_FILES = 500
MAX_RAW_FILE_BYTES = 10 * 1024 * 1024
MAX_RAW_TOTAL_BYTES = 100 * 1024 * 1024
REDACTED = "[REDACTED]"


class EvidenceBundleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BundleOptions:
    finding_ids: frozenset[str] = frozenset()
    redact_identifiers: bool = False
    redact_notes: bool = False
    redact_urls: bool = False
    include_raw: bool = True


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _redact_record(value: Any, options: BundleOptions) -> Any:
    if isinstance(value, list):
        return [_redact_record(item, options) for item in value]
    if not isinstance(value, dict):
        return value
    output: dict[str, Any] = {}
    identifier_keys = {
        "query",
        "queried_value",
        "value",
        "account_identifier",
        "username",
        "email",
        "phone",
        "search_value",
    }
    url_keys = {
        "canonical_url",
        "display_url",
        "source_url",
        "requested_url",
        "final_url",
        "avatar_url",
        "archive_url",
        "url",
    }
    note_keys = {"notes", "note", "analyst_note"}
    for key, item in value.items():
        key_folded = key.casefold()
        if options.redact_identifiers and key_folded in identifier_keys and item not in (None, ""):
            output[key] = REDACTED
        elif options.redact_urls and (key_folded in url_keys or key_folded.endswith("_url")) and item not in (None, ""):
            output[key] = REDACTED
        elif options.redact_notes and key_folded in note_keys and item not in (None, ""):
            output[key] = REDACTED
        else:
            output[key] = _redact_record(item, options)
    return output


def selective_investigation_dict(
    investigation: Investigation,
    options: BundleOptions,
) -> dict[str, Any]:
    selected_ids = options.finding_ids
    known_ids = {finding.id for finding in investigation.findings}
    unknown = selected_ids - known_ids
    if unknown:
        raise EvidenceBundleError("selected evidence contains findings outside this investigation")
    if len(selected_ids) > MAX_SELECTED_FINDINGS:
        raise EvidenceBundleError("too many findings selected for one evidence bundle")

    payload = investigation_dict(investigation)
    if selected_ids:
        payload["findings"] = [
            item for item in payload.get("findings", []) if item.get("id") in selected_ids
        ]
        payload["location_observations"] = [
            item
            for item in payload.get("location_observations", [])
            if item.get("finding_id") in selected_ids or item.get("finding_id") is None
        ]
        payload["account_view"] = {
            "omitted": True,
            "reason": "Account projection omitted from selective bundle to avoid referencing unselected findings.",
        }
        payload["summary"] = {
            **payload.get("summary", {}),
            "bundle_selected_findings": len(payload["findings"]),
            "bundle_selection_applied": True,
        }
    else:
        payload["summary"] = {
            **payload.get("summary", {}),
            "bundle_selected_findings": len(payload.get("findings", [])),
            "bundle_selection_applied": False,
        }
    payload["bundle_export"] = {
        "created_at": datetime.now(UTC).isoformat(),
        "redact_identifiers": options.redact_identifiers,
        "redact_notes": options.redact_notes,
        "redact_urls": options.redact_urls,
        "include_raw": options.include_raw,
        "selection": sorted(selected_ids),
    }
    return cast(dict[str, Any], _redact_record(payload, options))


def _iter_raw_files(data_dir: Path, investigation_id: str) -> list[Path]:
    root = (data_dir / "raw" / investigation_id).resolve()
    if not root.is_dir():
        return []
    files: list[Path] = []
    for candidate in sorted(root.rglob("*")):
        if len(files) >= MAX_RAW_FILES:
            break
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or root not in resolved.parents:
            continue
        files.append(resolved)
    return files


def build_evidence_bundle(
    investigation: Investigation,
    data_dir: Path,
    options: BundleOptions,
) -> bytes:
    case_payload = selective_investigation_dict(investigation, options)
    case_json = json.dumps(case_payload, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
    manifest_entries: list[dict[str, Any]] = [
        {
            "path": "case.json",
            "sha256": _sha256(case_json),
            "size_bytes": len(case_json),
            "kind": "structured_case_export",
        }
    ]
    raw_payloads: list[tuple[str, bytes]] = []
    raw_total = 0
    raw_omitted = 0
    if options.include_raw:
        raw_root = (data_dir / "raw" / investigation.id).resolve()
        for path in _iter_raw_files(data_dir, investigation.id):
            try:
                size = path.stat().st_size
            except OSError:
                raw_omitted += 1
                continue
            if size > MAX_RAW_FILE_BYTES or raw_total + size > MAX_RAW_TOTAL_BYTES:
                raw_omitted += 1
                continue
            try:
                content = path.read_bytes()
            except OSError:
                raw_omitted += 1
                continue
            relative = path.relative_to(raw_root).as_posix()
            archive_path = f"raw/{relative}"
            raw_payloads.append((archive_path, content))
            raw_total += len(content)
            manifest_entries.append(
                {
                    "path": archive_path,
                    "sha256": _sha256(content),
                    "size_bytes": len(content),
                    "kind": "retained_raw_evidence",
                }
            )

    manifest = {
        "schema_version": "1.0",
        "investigation_id": investigation.id,
        "created_at": datetime.now(UTC).isoformat(),
        "selection_applied": bool(options.finding_ids),
        "redactions": {
            "identifiers": options.redact_identifiers,
            "notes": options.redact_notes,
            "urls": options.redact_urls,
        },
        "raw_evidence": {
            "included": options.include_raw,
            "included_files": len(raw_payloads),
            "included_bytes": raw_total,
            "omitted_files_due_to_limits_or_errors": raw_omitted,
            "per_file_limit_bytes": MAX_RAW_FILE_BYTES,
            "total_limit_bytes": MAX_RAW_TOTAL_BYTES,
        },
        "files": manifest_entries,
    }
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("case.json", case_json)
        for archive_path, content in raw_payloads:
            archive.writestr(archive_path, content)
        archive.writestr("manifest.json", manifest_json)
    return output.getvalue()

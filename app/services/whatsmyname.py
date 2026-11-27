from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.upstreams import commit_for
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

WMN_REPOSITORY = "https://github.com/WebBreacher/WhatsMyName"
WMN_DATA_URL = "https://raw.githubusercontent.com/WebBreacher/WhatsMyName/{revision}/wmn-data.json"
WMN_LATEST_COMMIT_URL = "https://api.github.com/repos/WebBreacher/WhatsMyName/commits/main"
WMN_MAX_BYTES = 4 * 1024 * 1024
WMN_CACHE_NAME = "whatsmyname-dataset.json"
WMN_METADATA_NAME = "whatsmyname-dataset-metadata.json"


class WhatsMyNameError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    revision: str
    retrieved_at: str
    sha256: str
    site_count: int
    category_count: int
    categories: dict[str, int]
    protected_site_count: int
    schema_issue_count: int
    source_url: str


@dataclass(frozen=True, slots=True)
class CoverageComparison:
    pinned: DatasetSummary
    active: DatasetSummary
    added_sites: tuple[str, ...]
    removed_sites: tuple[str, ...]
    changed_sites: tuple[str, ...]


def _site_key(site: dict[str, Any]) -> str:
    return str(site.get("name") or site.get("uri_check") or "").strip().casefold()


def _validate_dataset(payload: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, dict):
        raise WhatsMyNameError("WhatsMyName dataset root must be an object")
    sites = payload.get("sites")
    categories = payload.get("categories")
    if not isinstance(sites, list) or not isinstance(categories, list):
        raise WhatsMyNameError("WhatsMyName dataset is missing sites or categories")
    if not sites or len(sites) > 5000:
        raise WhatsMyNameError("WhatsMyName site count is outside the accepted range")

    issues = 0
    seen: set[str] = set()
    for site in sites:
        if not isinstance(site, dict):
            issues += 1
            continue
        key = _site_key(site)
        uri = site.get("uri_check")
        category = site.get("cat")
        if not key or key in seen:
            issues += 1
        seen.add(key)
        if not isinstance(uri, str) or "{account}" not in uri:
            issues += 1
        if not isinstance(category, str) or not category.strip():
            issues += 1
    return payload, issues


def _summary(dataset: dict[str, Any], revision: str, retrieved_at: str, source_url: str) -> DatasetSummary:
    encoded = json.dumps(dataset, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sites = [site for site in dataset["sites"] if isinstance(site, dict)]
    category_counts = Counter(str(site.get("cat") or "uncategorised") for site in sites)
    _, issue_count = _validate_dataset(dataset)
    return DatasetSummary(
        revision=revision,
        retrieved_at=retrieved_at,
        sha256=hashlib.sha256(encoded).hexdigest(),
        site_count=len(sites),
        category_count=len(category_counts),
        categories=dict(sorted(category_counts.items())),
        protected_site_count=sum(bool(site.get("protection")) for site in sites),
        schema_issue_count=issue_count,
        source_url=source_url,
    )


async def _download_json(url: str, *, max_bytes: int = WMN_MAX_BYTES) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "OSINT-Workbench/WhatsMyName-Coverage"}
    try:
        async with safe_client(
            timeout=30,
            user_agent=headers["User-Agent"],
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content = await read_bounded_response(response, max_bytes=max_bytes)
    except EgressPolicyError as exc:
        raise WhatsMyNameError("WhatsMyName response exceeds the size limit") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise WhatsMyNameError("WhatsMyName returned malformed JSON") from exc


async def latest_revision() -> str:
    payload = await _download_json(WMN_LATEST_COMMIT_URL, max_bytes=512 * 1024)
    revision = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(revision, str) or not revision.strip():
        raise WhatsMyNameError("GitHub did not return a WhatsMyName revision")
    return revision.strip()


async def fetch_dataset(revision: str) -> tuple[dict[str, Any], DatasetSummary]:
    revision = revision.strip()
    if not revision or len(revision) > 64 or not all(char.isalnum() for char in revision):
        raise WhatsMyNameError("invalid WhatsMyName revision")
    source_url = WMN_DATA_URL.format(revision=revision)
    dataset, _ = _validate_dataset(await _download_json(source_url))
    retrieved_at = datetime.now(UTC).isoformat()
    return dataset, _summary(dataset, revision, retrieved_at, source_url)


def cached_dataset(data_dir: Path) -> tuple[dict[str, Any], DatasetSummary] | None:
    dataset_path = data_dir / WMN_CACHE_NAME
    metadata_path = data_dir / WMN_METADATA_NAME
    if not dataset_path.is_file() or not metadata_path.is_file():
        return None
    try:
        dataset, _ = _validate_dataset(json.loads(dataset_path.read_text(encoding="utf-8")))
        metadata = DatasetSummary(**json.loads(metadata_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, WhatsMyNameError):
        return None
    return dataset, metadata


def store_dataset(data_dir: Path, dataset: dict[str, Any], summary: DatasetSummary) -> None:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dataset_path = data_dir / WMN_CACHE_NAME
    metadata_path = data_dir / WMN_METADATA_NAME
    dataset_path.write_text(json.dumps(dataset, indent=2, sort_keys=True), encoding="utf-8")
    metadata_path.write_text(json.dumps(asdict(summary), indent=2, sort_keys=True), encoding="utf-8")
    dataset_path.chmod(0o600)
    metadata_path.chmod(0o600)


async def coverage_status(data_dir: Path) -> CoverageComparison:
    pinned_revision = commit_for("whatsmyname")
    if not pinned_revision:
        raise WhatsMyNameError("WhatsMyName is missing from the upstream lock")
    pinned_data, pinned_summary = await fetch_dataset(pinned_revision)
    active = cached_dataset(data_dir)
    if active is None:
        active_data, active_summary = pinned_data, pinned_summary
    else:
        active_data, active_summary = active

    pinned_sites = {_site_key(site): site for site in pinned_data["sites"] if isinstance(site, dict)}
    active_sites = {_site_key(site): site for site in active_data["sites"] if isinstance(site, dict)}
    shared = pinned_sites.keys() & active_sites.keys()
    return CoverageComparison(
        pinned=pinned_summary,
        active=active_summary,
        added_sites=tuple(sorted(active_sites.keys() - pinned_sites.keys())),
        removed_sites=tuple(sorted(pinned_sites.keys() - active_sites.keys())),
        changed_sites=tuple(
            sorted(key for key in shared if pinned_sites[key] != active_sites[key])
        ),
    )


async def refresh_dataset(data_dir: Path) -> CoverageComparison:
    revision = await latest_revision()
    dataset, summary = await fetch_dataset(revision)
    store_dataset(data_dir, dataset, summary)
    return await coverage_status(data_dir)

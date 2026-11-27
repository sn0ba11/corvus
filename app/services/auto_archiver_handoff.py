"""Build analyst-approved handoff bundles for Bellingcat Auto Archiver."""

import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

from app.services.wayback import normalise_save_url

AUTO_ARCHIVER_VERSION = "1.2.7"
AUTO_ARCHIVER_COMMIT = "afbe4fac508524d282328194ab1fa8d31cca6751"
MAX_ARCHIVE_CANDIDATES = 50


@dataclass(frozen=True, slots=True)
class AutoArchiverCandidate:
    finding_id: str
    url: str
    platform: str | None
    status: str
    source_tools: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class AutoArchiverBundle:
    filename: str
    content: bytes
    sha256: str
    candidate_count: int
    manifest_sha256: str


def _utc_iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _csv_bytes(investigation_id: str, candidates: tuple[AutoArchiverCandidate, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=(
            "url",
            "investigation_id",
            "finding_id",
            "platform",
            "status",
            "source_tools",
            "observed_at_utc",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    for candidate in candidates:
        writer.writerow(
            {
                "url": candidate.url,
                "investigation_id": investigation_id,
                "finding_id": candidate.finding_id,
                "platform": candidate.platform or "",
                "status": candidate.status,
                "source_tools": ";".join(candidate.source_tools),
                "observed_at_utc": _utc_iso(candidate.observed_at),
            }
        )
    return buffer.getvalue().encode("utf-8")


def _orchestration_yaml() -> bytes:
    return b"""# Corvus handoff for Bellingcat Auto Archiver 1.2.7
steps:
  feeders:
  - csv_feeder
  extractors:
  - generic_extractor
  - antibot_extractor_enricher
  enrichers:
  - hash_enricher
  - metadata_enricher
  - ssl_enricher
  databases:
  - console_db
  - csv_db
  storages:
  - local_storage
  formatters:
  - html_formatter

authentication: {}
logging:
  level: INFO

csv_feeder:
  files:
  - urls.csv
  column: url
csv_db:
  csv_file: results.csv
local_storage:
  path_generator: url
  filename_generator: static
  save_to: ./archive
  save_absolute: false
generic_extractor:
  subtitles: true
  comments: false
  livestreams: false
  live_from_start: false
  proxy: ''
  end_means_success: true
  allow_playlist: false
  max_downloads: 1
  ytdlp_update_interval: -1
antibot_extractor_enricher:
  save_to_pdf: true
  max_download_images: 10
  max_download_videos: 2
  detect_auth_wall: true
hash_enricher:
  algorithm: SHA-256
  chunksize: 16000000
html_formatter:
  detect_thumbnails: true
"""


def _requirements() -> bytes:
    return f"auto-archiver=={AUTO_ARCHIVER_VERSION}\n".encode()


def _readme() -> bytes:
    return f"""Corvus → Bellingcat Auto Archiver handoff

This bundle was generated only after an analyst selected a public finding URL.
Creating the bundle did not contact Bellingcat, the target site or an archive service.

Pinned integration reference
- Auto Archiver version: {AUTO_ARCHIVER_VERSION}
- Official source commit: {AUTO_ARCHIVER_COMMIT}

Run in a separate Python 3.10–3.12 environment:

  python -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/auto-archiver --mode full --config orchestration.yaml

On Windows PowerShell use:

  py -3.12 -m venv .venv
  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt
  .\\.venv\\Scripts\\auto-archiver.exe --mode full --config orchestration.yaml

Outputs are written beneath ./archive and to results.csv. Review authentication,
service terms, storage capacity and the generated configuration before running it.
The anti-bot extractor is configured conservatively, rejects detected login walls and
limits downloaded page media. Authentication is deliberately empty.

Verify this bundle before use:

  sha256sum -c SHA256SUMS

The manifest links every URL back to its Corvus finding and collection tools.
""".encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_filename_component(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return clean[:60] or "investigation"


def build_auto_archiver_bundle(
    *,
    investigation_id: str,
    investigation_title: str,
    candidates: tuple[AutoArchiverCandidate, ...],
    generated_at: datetime | None = None,
) -> AutoArchiverBundle:
    """Create a provenance-preserving ZIP accepted by Auto Archiver's CSV feeder."""
    if not candidates:
        raise ValueError("select at least one finding URL to archive")
    if len(candidates) > MAX_ARCHIVE_CANDIDATES:
        raise ValueError(f"select no more than {MAX_ARCHIVE_CANDIDATES} finding URLs")

    clean_candidates: list[AutoArchiverCandidate] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        if candidate.status not in {"found", "likely"}:
            raise ValueError("only positive or likely findings can be handed to Auto Archiver")
        clean_url = normalise_save_url(candidate.url)
        if clean_url in seen_urls:
            continue
        seen_urls.add(clean_url)
        clean_candidates.append(
            AutoArchiverCandidate(
                finding_id=candidate.finding_id,
                url=clean_url,
                platform=candidate.platform,
                status=candidate.status,
                source_tools=tuple(dict.fromkeys(candidate.source_tools)),
                observed_at=candidate.observed_at,
            )
        )
    if not clean_candidates:
        raise ValueError("no unique public finding URLs were selected")

    created_at = (generated_at or datetime.now(UTC)).astimezone(UTC)
    selected = tuple(clean_candidates)
    urls_csv = _csv_bytes(investigation_id, selected)

    files: dict[str, bytes] = {
        "urls.csv": urls_csv,
        "orchestration.yaml": _orchestration_yaml(),
        "requirements.txt": _requirements(),
        "README.txt": _readme(),
    }
    manifest = {
        "schema": "osint-workbench-auto-archiver-handoff/v1",
        "generated_at_utc": created_at.isoformat(),
        "investigation": {
            "id": investigation_id,
            "title": investigation_title,
        },
        "auto_archiver": {
            "version": AUTO_ARCHIVER_VERSION,
            "commit": AUTO_ARCHIVER_COMMIT,
            "repository": "https://github.com/bellingcat/auto-archiver",
        },
        "candidate_count": len(selected),
        "candidates": [
            {
                "finding_id": candidate.finding_id,
                "url": candidate.url,
                "platform": candidate.platform,
                "status": candidate.status,
                "source_tools": candidate.source_tools,
                "observed_at_utc": _utc_iso(candidate.observed_at),
            }
            for candidate in selected
        ],
        "files": {
            name: {"sha256": _sha256(content), "bytes": len(content)}
            for name, content in files.items()
        },
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    files["bundle-manifest.json"] = manifest_bytes
    checksum_lines = [f"{_sha256(content)}  {name}" for name, content in sorted(files.items())]
    files["SHA256SUMS"] = ("\n".join(checksum_lines) + "\n").encode()

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.date_time = (
                created_at.year,
                created_at.month,
                created_at.day,
                created_at.hour,
                created_at.minute,
                created_at.second,
            )
            info.external_attr = 0o600 << 16
            output.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    payload = archive.getvalue()
    title = _safe_filename_component(investigation_title)
    return AutoArchiverBundle(
        filename=f"OSINT-Auto-Archiver-{title}-{investigation_id[:8]}.zip",
        content=payload,
        sha256=_sha256(payload),
        candidate_count=len(selected),
        manifest_sha256=_sha256(manifest_bytes),
    )

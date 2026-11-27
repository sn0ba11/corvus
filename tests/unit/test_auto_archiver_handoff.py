import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest

from app.services.auto_archiver_handoff import (
    AUTO_ARCHIVER_COMMIT,
    AUTO_ARCHIVER_VERSION,
    AutoArchiverCandidate,
    build_auto_archiver_bundle,
)


def candidate(
    finding_id: str,
    url: str,
    *,
    status: str = "found",
) -> AutoArchiverCandidate:
    return AutoArchiverCandidate(
        finding_id=finding_id,
        url=url,
        platform="Example",
        status=status,
        source_tools=("maigret", "sherlock", "maigret"),
        observed_at=datetime(2026, 8, 1, 12, 30, tzinfo=UTC),
    )


def test_bundle_contains_csv_configuration_manifest_and_checksums() -> None:
    bundle = build_auto_archiver_bundle(
        investigation_id="case-12345678",
        investigation_title="Example Case",
        candidates=(
            candidate("finding-one", "https://example.org/profile#fragment"),
            candidate("finding-two", "https://example.org/profile"),
        ),
        generated_at=datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
    )

    assert bundle.filename == "OSINT-Auto-Archiver-Example-Case-case-123.zip"
    assert bundle.sha256 == hashlib.sha256(bundle.content).hexdigest()
    assert bundle.candidate_count == 1
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        assert names == {
            "README.txt",
            "SHA256SUMS",
            "bundle-manifest.json",
            "orchestration.yaml",
            "requirements.txt",
            "urls.csv",
        }
        rows = list(csv.DictReader(io.StringIO(archive.read("urls.csv").decode())))
        assert rows == [
            {
                "url": "https://example.org/profile",
                "investigation_id": "case-12345678",
                "finding_id": "finding-one",
                "platform": "Example",
                "status": "found",
                "source_tools": "maigret;sherlock",
                "observed_at_utc": "2026-08-01T12:30:00+00:00",
            }
        ]
        manifest_bytes = archive.read("bundle-manifest.json")
        manifest = json.loads(manifest_bytes)
        assert manifest["auto_archiver"] == {
            "version": AUTO_ARCHIVER_VERSION,
            "commit": AUTO_ARCHIVER_COMMIT,
            "repository": "https://github.com/bellingcat/auto-archiver",
        }
        assert manifest["candidate_count"] == 1
        assert bundle.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()
        assert (
            f"auto-archiver=={AUTO_ARCHIVER_VERSION}" in archive.read("requirements.txt").decode()
        )
        orchestration = archive.read("orchestration.yaml").decode()
        assert "- csv_feeder" in orchestration
        assert "column: url" in orchestration
        assert "algorithm: SHA-256" in orchestration
        assert "ytdlp_update_interval: -1" in orchestration
        checksums = {
            line.split("  ", 1)[1]: line.split("  ", 1)[0]
            for line in archive.read("SHA256SUMS").decode().splitlines()
        }
        for name, digest in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest


def test_bundle_rejects_non_positive_or_private_targets() -> None:
    with pytest.raises(ValueError, match="positive or likely"):
        build_auto_archiver_bundle(
            investigation_id="case",
            investigation_title="Case",
            candidates=(candidate("one", "https://example.org", status="unknown"),),
        )
    with pytest.raises(ValueError, match="private or non-routable"):
        build_auto_archiver_bundle(
            investigation_id="case",
            investigation_title="Case",
            candidates=(candidate("one", "http://127.0.0.1/private"),),
        )

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.models.schemas import FindingRecord, QueryType

PROFILE_FINDING_TYPES = frozenset({"profile", "instagram_profile", "unverified_profile_lead"})


def normalise_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", "", value).casefold()


def canonical_url_score(value: str | None) -> int:
    if not value:
        return -100
    parsed = urlsplit(value)
    path = parsed.path.casefold()
    score = 0
    if not parsed.query:
        score += 3
    if "/profile/" in path:
        score += 4
    if parsed.query:
        score -= 3
    if "/api/" in path or re.search(r"/\d{4}-\d{2}-\d{2}/users$", path):
        score -= 3
    return score


def profile_identity_key(finding: FindingRecord) -> tuple[str, ...] | None:
    if finding.finding_type not in PROFILE_FINDING_TYPES or not finding.platform:
        return None
    candidates: list[str | None] = []
    if finding.query_type == QueryType.USERNAME:
        candidates.append(finding.queried_value)
    candidates.extend((finding.profile.account_id, finding.value))
    for candidate in candidates:
        identifier = normalise_identifier(candidate)
        if identifier and not identifier.startswith(("http://", "https://")):
            return ("profile", finding.platform.strip().casefold(), identifier)
    return None


def dedupe_key(finding: FindingRecord) -> tuple[str, ...]:
    profile_key = profile_identity_key(finding)
    if profile_key is not None:
        return profile_key
    if finding.canonical_url:
        return ("url", finding.canonical_url)
    identifier = normalise_identifier(finding.value)
    if finding.platform and identifier:
        return ("platform", finding.platform.casefold(), identifier)
    if finding.source_record_id:
        return ("source", finding.source_tool, finding.source_record_id)
    payload = finding.model_dump(mode="json", exclude={"observed_at"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return ("content", finding.source_tool, hashlib.sha256(encoded).hexdigest())


@dataclass(slots=True)
class MergedFinding:
    finding: FindingRecord
    sources: list[str] = field(default_factory=list)


def deduplicate(findings: list[FindingRecord]) -> list[MergedFinding]:
    merged: dict[tuple[str, ...], MergedFinding] = {}
    for finding in findings:
        key = dedupe_key(finding)
        if key not in merged:
            merged[key] = MergedFinding(finding, [finding.source_tool])
        elif finding.source_tool not in merged[key].sources:
            merged[key].sources.append(finding.source_tool)
    return list(merged.values())

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import canonicalise_url
from app.models.db import Investigation, LocationObservation
from app.models.schemas import LocationEvidenceType, LocationObservationInput

DEFAULT_CONFIDENCE: dict[LocationEvidenceType, float] = {
    LocationEvidenceType.DECLARED: 0.35,
    LocationEvidenceType.REVIEW: 0.65,
    LocationEvidenceType.FITNESS_ACTIVITY: 0.75,
    LocationEvidenceType.EVENT: 0.70,
    LocationEvidenceType.PHOTO: 0.80,
    LocationEvidenceType.TEXT_MENTION: 0.40,
}


@dataclass(frozen=True)
class LocationCluster:
    place_name: str
    locality: str | None
    region: str | None
    country: str | None
    observation_count: int
    evidence_types: tuple[str, ...]
    source_platforms: tuple[str, ...]
    first_observed_at: datetime
    last_observed_at: datetime
    earliest_occurred_at: datetime | None
    latest_occurred_at: datetime | None
    maximum_confidence: float
    privacy_masked_count: int
    classification: str


def _normalise_place(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def validate_source_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError("source URL must be a public HTTP(S) URL") from exc
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("source URL must not contain credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("source URL must use a public host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("source URL must use a public host")
    canonical = canonicalise_url(value)
    if canonical is None:
        raise ValueError("source URL must be a public HTTP(S) URL")
    return canonical


def confidence_for(payload: LocationObservationInput) -> float:
    return (
        payload.confidence
        if payload.confidence is not None
        else DEFAULT_CONFIDENCE[payload.evidence_type]
    )


def add_location_observation(
    db: Session,
    investigation: Investigation,
    payload: LocationObservationInput,
    *,
    finding_id: str | None = None,
    observed_at: datetime | None = None,
) -> LocationObservation:
    now = observed_at or datetime.now(UTC)
    source_url = validate_source_url(payload.source_url)
    duplicate = db.scalar(
        select(LocationObservation).where(
            LocationObservation.investigation_id == investigation.id,
            LocationObservation.finding_id == finding_id,
            LocationObservation.evidence_type == payload.evidence_type.value,
            LocationObservation.place_name == payload.place_name,
            LocationObservation.source_platform == payload.source_platform,
            LocationObservation.source_url == source_url,
            LocationObservation.occurred_at == payload.occurred_at,
        )
    )
    if duplicate:
        duplicate.last_observed_at = now
        return duplicate

    item = LocationObservation(
        investigation_id=investigation.id,
        finding_id=finding_id,
        place_name=payload.place_name,
        locality=payload.locality,
        region=payload.region,
        country=payload.country,
        latitude=payload.latitude,
        longitude=payload.longitude,
        precision_radius_m=payload.precision_radius_m,
        evidence_type=payload.evidence_type.value,
        occurred_at=payload.occurred_at,
        published_at=payload.published_at,
        first_observed_at=now,
        last_observed_at=now,
        source_platform=payload.source_platform,
        source_url=source_url,
        account_identifier=payload.account_identifier,
        privacy_masked=payload.privacy_masked,
        confidence=confidence_for(payload),
        raw_evidence=payload.raw_evidence,
    )
    db.add(item)
    return item


def seed_declared_locations(db: Session, investigation: Investigation) -> int:
    added = 0
    for finding in investigation.findings:
        profile = finding.extra.get("profile", {})
        if not isinstance(profile, dict):
            continue
        location = profile.get("location")
        if not isinstance(location, str) or not location.strip():
            continue
        existing = db.scalar(
            select(LocationObservation).where(
                LocationObservation.investigation_id == investigation.id,
                LocationObservation.finding_id == finding.id,
                LocationObservation.evidence_type == LocationEvidenceType.DECLARED.value,
            )
        )
        if existing:
            existing.last_observed_at = finding.observed_at
            continue
        payload = LocationObservationInput(
            place_name=location,
            evidence_type=LocationEvidenceType.DECLARED,
            source_platform=finding.platform or finding.source_tool,
            source_url=finding.canonical_url,
            account_identifier=(
                str(profile.get("account_id"))
                if profile.get("account_id") is not None
                else finding.value
            ),
            raw_evidence={"source_field": "profile.location"},
        )
        add_location_observation(
            db,
            investigation,
            payload,
            finding_id=finding.id,
            observed_at=finding.observed_at,
        )
        added += 1
    return added


def build_location_clusters(observations: list[LocationObservation]) -> list[LocationCluster]:
    grouped: dict[tuple[str, str, str, str], list[LocationObservation]] = {}
    for observation in observations:
        key = (
            _normalise_place(observation.place_name),
            _normalise_place(observation.locality or ""),
            _normalise_place(observation.region or ""),
            _normalise_place(observation.country or ""),
        )
        grouped.setdefault(key, []).append(observation)

    clusters: list[LocationCluster] = []
    for entries in grouped.values():
        first_observed = min(_aware(item.first_observed_at) for item in entries)
        last_observed = max(_aware(item.last_observed_at) for item in entries)
        occurred = sorted(_aware(item.occurred_at) for item in entries if item.occurred_at)
        platforms = tuple(sorted({item.source_platform for item in entries}))
        evidence_types = tuple(sorted({item.evidence_type for item in entries}))
        if len(platforms) >= 2:
            classification = "corroborated"
        elif len(entries) >= 3:
            classification = "recurring"
        elif len(entries) >= 2:
            classification = "repeated"
        else:
            classification = "single_observation"
        representative = entries[0]
        clusters.append(
            LocationCluster(
                place_name=representative.place_name,
                locality=representative.locality,
                region=representative.region,
                country=representative.country,
                observation_count=len(entries),
                evidence_types=evidence_types,
                source_platforms=platforms,
                first_observed_at=first_observed,
                last_observed_at=last_observed,
                earliest_occurred_at=occurred[0] if occurred else None,
                latest_occurred_at=occurred[-1] if occurred else None,
                maximum_confidence=max(item.confidence for item in entries),
                privacy_masked_count=sum(1 for item in entries if item.privacy_masked),
                classification=classification,
            )
        )
    return sorted(
        clusters,
        key=lambda item: (
            -item.observation_count,
            -len(item.source_platforms),
            item.place_name.casefold(),
        ),
    )

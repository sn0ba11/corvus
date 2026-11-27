from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import Base, Finding, Investigation, LocationObservation, ToolRun
from app.models.schemas import LocationEvidenceType, LocationObservationInput
from app.services.location_intelligence import (
    add_location_observation,
    build_location_clusters,
    confidence_for,
    seed_declared_locations,
    validate_source_url,
)


def investigation() -> Investigation:
    return Investigation(
        title="Location test",
        query="johnsmith",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
    )


def test_evidence_types_have_conservative_default_confidence() -> None:
    review = LocationObservationInput(
        place_name="Dublin",
        evidence_type=LocationEvidenceType.REVIEW,
        source_platform="Google Maps",
    )
    fitness = LocationObservationInput(
        place_name="Phoenix Park",
        evidence_type=LocationEvidenceType.FITNESS_ACTIVITY,
        source_platform="Strava",
        privacy_masked=True,
    )
    declared = LocationObservationInput(
        place_name="Dublin",
        evidence_type=LocationEvidenceType.DECLARED,
        source_platform="Example",
    )
    assert confidence_for(review) == 0.65
    assert confidence_for(fitness) == 0.75
    assert confidence_for(declared) == 0.35


def test_location_source_urls_reject_credentials_and_local_hosts() -> None:
    assert validate_source_url("https://strava.com/activities/123?utm_source=test") == (
        "https://strava.com/activities/123"
    )
    with pytest.raises(ValueError, match="credentials"):
        validate_source_url("https://user:secret@example.org/review")
    with pytest.raises(ValueError, match="public host"):
        validate_source_url("http://127.0.0.1/review")
    with pytest.raises(ValueError, match="public host"):
        validate_source_url("http://service.internal/review")


def test_declared_profile_locations_are_promoted_once() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        item = investigation()
        run = ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
        )
        finding = Finding(
            query_type="username",
            queried_value="johnsmith",
            finding_type="profile",
            platform="Example Social",
            value="johnsmith",
            canonical_url="https://example.org/johnsmith",
            display_url="https://example.org/johnsmith",
            status="found",
            extra={"profile": {"location": "Dublin", "account_id": "123"}},
            source_tool="maigret",
            source_tools=["maigret"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        item.runs.append(run)
        item.findings.append(finding)
        run.findings.append(finding)
        db.add(item)
        db.flush()

        assert seed_declared_locations(db, item) == 1
        db.flush()
        assert seed_declared_locations(db, item) == 0
        observations = db.query(LocationObservation).all()

    assert len(observations) == 1
    assert observations[0].place_name == "Dublin"
    assert observations[0].evidence_type == "declared"
    assert observations[0].confidence == 0.35
    assert observations[0].account_identifier == "123"


def test_location_clusters_corroborate_independent_platforms_without_claiming_residence() -> None:
    first = LocationObservation(
        investigation_id="investigation",
        place_name="Dublin",
        locality="Dublin",
        evidence_type="review",
        occurred_at=datetime(2024, 1, 10, tzinfo=UTC),
        first_observed_at=datetime(2024, 1, 11, tzinfo=UTC),
        last_observed_at=datetime(2024, 1, 11, tzinfo=UTC),
        source_platform="Google Maps",
        confidence=0.65,
        privacy_masked=False,
        raw_evidence={},
    )
    second = LocationObservation(
        investigation_id="investigation",
        place_name=" dublin ",
        locality="DUBLIN",
        evidence_type="fitness_activity",
        occurred_at=datetime(2025, 6, 20, tzinfo=UTC),
        first_observed_at=datetime(2025, 6, 21, tzinfo=UTC),
        last_observed_at=datetime(2025, 6, 21, tzinfo=UTC),
        source_platform="Strava",
        confidence=0.75,
        privacy_masked=True,
        raw_evidence={},
    )

    cluster = build_location_clusters([first, second])[0]
    assert cluster.classification == "corroborated"
    assert cluster.observation_count == 2
    assert cluster.source_platforms == ("Google Maps", "Strava")
    assert cluster.evidence_types == ("fitness_activity", "review")
    assert cluster.privacy_masked_count == 1
    assert cluster.earliest_occurred_at == datetime(2024, 1, 10, tzinfo=UTC)
    assert cluster.latest_occurred_at == datetime(2025, 6, 20, tzinfo=UTC)


def test_duplicate_manual_observation_updates_last_seen() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        item = investigation()
        db.add(item)
        db.flush()
        payload = LocationObservationInput(
            place_name="Phoenix Park",
            evidence_type=LocationEvidenceType.FITNESS_ACTIVITY,
            occurred_at=datetime(2026, 7, 1, tzinfo=UTC),
            source_platform="Strava",
            source_url="https://strava.com/activities/123?utm_source=test",
            privacy_masked=True,
        )
        first = add_location_observation(
            db, item, payload, observed_at=datetime(2026, 7, 2, tzinfo=UTC)
        )
        db.flush()
        second = add_location_observation(
            db, item, payload, observed_at=datetime(2026, 7, 3, tzinfo=UTC)
        )
        db.flush()
        count = db.query(LocationObservation).count()

    assert first.id == second.id
    assert count == 1
    assert second.last_observed_at == datetime(2026, 7, 3, tzinfo=UTC)
    assert second.source_url == "https://strava.com/activities/123"

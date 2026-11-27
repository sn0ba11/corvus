from datetime import UTC, datetime

from app.models.db import Finding, Investigation
from app.services.evidence_clusters import (
    build_evidence_clusters,
    update_cluster_note,
    update_pair_override,
)


def _finding(finding_id: str, platform: str) -> Finding:
    return Finding(
        id=finding_id,
        tool_run_id=f"run-{finding_id}",
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform=platform,
        value="example",
        status="found",
        confidence=0.9,
        extra={"profile": {"display_name": "Example Person", "account_id": "example"}},
        source_tool=platform.casefold(),
        source_tools=[platform.casefold()],
        observed_at=datetime(2026, 8, 17, tzinfo=UTC),
    )


def test_clusters_join_strong_pairs_and_expose_rationale() -> None:
    investigation = Investigation(
        id="case-one",
        title="Clusters",
        query="example",
        query_type="username",
        selected_tools=[],
        tags=[],
        status="completed",
    )
    left = _finding("left", "GitHub")
    right = _finding("right", "Reddit")
    left.extra["account_correlation"] = {
        "matches": [
            {
                "peer_id": "right",
                "score": 82,
                "decision": "unreviewed",
                "signals": [
                    {"type": "username_exact", "weight": 35},
                    {"type": "display_name", "weight": 20},
                ],
            }
        ]
    }
    investigation.findings.extend([left, right])

    clusters = build_evidence_clusters(investigation)

    assert len(clusters) == 1
    assert clusters[0].finding_ids == ("left", "right")
    assert clusters[0].strongest_score == 82
    assert {item["signal"] for item in clusters[0].rationale} == {
        "username_exact",
        "display_name",
    }


def test_split_merge_and_notes_are_projection_only_controls() -> None:
    investigation = Investigation(
        id="case-two",
        title="Clusters",
        query="example",
        query_type="username",
        selected_tools=[],
        tags=[],
        status="completed",
    )
    left = _finding("left", "GitHub")
    right = _finding("right", "Reddit")
    investigation.findings.extend([left, right])
    valid = {"left", "right"}

    merged = update_pair_override(None, valid, "left", "right", "merge")
    clusters = build_evidence_clusters(investigation, merged)
    assert len(clusters) == 1
    cluster_id = clusters[0].id

    noted = update_cluster_note(merged, valid, cluster_id, "Likely same operator; verify manually")
    assert build_evidence_clusters(investigation, noted)[0].note == "Likely same operator; verify manually"

    split = update_pair_override(noted, valid, "left", "right", "split")
    assert build_evidence_clusters(investigation, split) == []
    assert left.extra.get("account_correlation") is None
    assert right.extra.get("account_correlation") is None

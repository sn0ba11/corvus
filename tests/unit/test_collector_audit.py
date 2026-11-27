import json
from pathlib import Path

from app.services import collector_audit
from app.services.collector_audit import (
    build_audit_presentation,
    render_html,
    render_text,
    reparse_audit,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _finding(
    *,
    tool: str,
    platform: str,
    url: str,
    status: str = "found",
    profile: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "query_type": "username",
        "queried_value": "johnsmith",
        "finding_type": "profile",
        "platform": platform,
        "value": "johnsmith",
        "canonical_url": url,
        "display_url": url,
        "status": status,
        "confidence": 0.8 if status == "found" else 0.15,
        "title": None,
        "description": None,
        "metadata": metadata or {},
        "profile": profile or {},
        "source_tool": tool,
        "source_record_id": f"{tool}-{platform}",
        "observed_at": "2026-08-05T12:00:00Z",
        "raw_output_reference": "raw",
    }


def test_fallback_account_key_is_stable_for_equivalent_records() -> None:
    left = {
        "source_tool": "example",
        "metadata": {"two": 2, "one": 1},
        "status": "unknown",
    }
    right = {
        "status": "unknown",
        "metadata": {"one": 1, "two": 2},
        "source_tool": "example",
    }

    assert collector_audit._account_key(left) == collector_audit._account_key(right)


def test_nested_metadata_gap_scan_is_bounded_and_tolerates_malformed_entries() -> None:
    entries: list[object] = [None, "bad", {"name": ["bad"], "value": "ignored"}]
    entries.extend({"name": "response time", "value": index} for index in range(197))
    entries.append({"name": "email", "value": "outside-bound@example.test"})
    record = _finding(tool="blackbird", platform="Example", url="https://example.test",
                      metadata={"metadata": entries})
    assert collector_audit.analyse_parsing_gaps([record])["field_count"] == 0


def test_presentation_merges_sources_extracts_entities_and_builds_timeline(tmp_path: Path) -> None:
    _write(
        tmp_path / "run.json",
        {
            "query": "johnsmith",
            "query_type": "username",
            "tools": ["maigret", "social_analyzer"],
        },
    )
    for tool in ("maigret", "social_analyzer"):
        _write(
            tmp_path / tool / "summary.json",
            {
                "tool": tool,
                "display_name": tool.replace("_", " ").title(),
                "state": "completed",
                "normalised_count": 1,
                "found_count": 1 if tool == "maigret" else 0,
                "unknown_count": 0 if tool == "maigret" else 1,
            },
        )

    _write(
        tmp_path / "maigret" / "normalised.json",
        [
            _finding(
                tool="maigret",
                platform="GitHub",
                url="https://github.com/johnsmith",
                profile={
                    "avatar_url": "https://avatars.example/johnsmith.png",
                    "display_name": "John",
                    "account_id": "johnsmith",
                    "biography": "<script>alert(1)</script>",
                    "followers": 18,
                    "following": 0,
                    "location": "Dublin, Ireland",
                },
                metadata={
                    "platform_details": [
                        {"label": "Account ID", "value": "296614"},
                        {"label": "Public email", "value": "j***@example.test"},
                    ],
                    "timeline_events": [
                        {
                            "type": "registered",
                            "label": "Account registered",
                            "timestamp": "2010-06-04T14:31:24+00:00",
                            "semantic": "account_creation",
                            "category": "account",
                            "confidence": "high",
                            "include_in_timeline": True,
                            "source_field": "created_at",
                            "raw_value": "2010-06-04T14:31:24Z",
                        },
                        {
                            "type": "feed_generated",
                            "label": "RSS feed generated",
                            "timestamp": "2026-08-05T11:02:46+00:00",
                            "semantic": "feed_generation",
                            "category": "observation",
                            "confidence": "low",
                            "include_in_timeline": False,
                            "source_field": "latest_activity_at",
                            "raw_value": "2026-08-05 11:02:46 UTC",
                            "note": "Feed build time is not user activity.",
                        },
                    ],
                },
            )
        ],
    )
    _write(
        tmp_path / "social_analyzer" / "normalised.json",
        [
            _finding(
                tool="social_analyzer",
                platform="GitHub",
                url="https://github.com/johnsmith",
                status="unknown",
                profile={
                    "display_name": "John",
                    "account_id": "johnsmith",
                    "followers": 17,
                },
            )
        ],
    )

    presentation = build_audit_presentation(tmp_path)

    assert presentation["summary"]["raw_record_count"] == 2
    assert presentation["summary"]["account_count"] == 1
    assert presentation["summary"]["corroborated_account_count"] == 1
    assert presentation["summary"]["timeline_event_count"] == 1
    assert presentation["summary"]["excluded_timeline_observation_count"] == 1

    account = presentation["accounts"][0]
    assert account["sources"] == ["maigret", "social_analyzer"]
    assert account["classification"] == "corroborated"
    assert account["profile"]["followers"] == 18
    assert account["status"] == "found"
    assert len(account["timeline_events"]) == 2
    details = {item["label"]: item["value"] for item in account["details"]}
    assert details["Last observed by Corvus"] == "2026-08-05T12:00:00Z"

    entities = presentation["entities"]
    assert entities["usernames"][0]["value"] == "johnsmith"
    assert entities["names"][0]["value"] == "John"
    assert entities["locations"][0]["value"] == "Dublin, Ireland"
    assert entities["ids"][0]["value"] == "296614"
    assert entities["emails"][0]["value"] == "j***@example.test"
    assert entities["images"][0]["value"] == "https://avatars.example/johnsmith.png"

    event = presentation["timeline"][0]
    assert event["platform"] == "GitHub"
    assert event["type"] == "registered"

    text = render_text(presentation, card_limit=10)
    assert "ACCOUNT CARDS" in text
    assert "GitHub · found · corroborated" in text
    assert "GROUPED ENTITIES" in text
    assert "COMBINED TIMELINE" in text
    assert "1 excluded observations" in text
    assert "Excluded observation — RSS feed generated" in text

    html = render_html(presentation, card_limit=10)
    assert "data-account-card" in html
    assert "Excluded date observations" in html
    assert "RSS feed generated" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'src="/static/corvus-mark.jpg?v=corvus-raster-2"' in html
    assert 'src="https://avatars.example/johnsmith.png"' not in html


def test_parser_gap_review_flags_unpresented_identity_metadata(tmp_path: Path) -> None:
    _write(
        tmp_path / "run.json",
        {"query": "johnsmith", "query_type": "username", "tools": ["maigret"]},
    )
    _write(
        tmp_path / "maigret" / "summary.json",
        {"tool": "maigret", "state": "completed", "normalised_count": 1},
    )
    _write(
        tmp_path / "maigret" / "normalised.json",
        [
            _finding(
                tool="maigret",
                platform="Example",
                url="https://example.test/johnsmith",
                profile={"account_id": "johnsmith"},
                metadata={
                    "phone_hint": "+353 *** 1234",
                    "response_time_s": 1.25,
                    "sherlock_unverified_claim": True,
                },
            )
        ],
    )

    presentation = build_audit_presentation(tmp_path)

    assert presentation["summary"]["parser_gap_field_count"] == 1
    assert presentation["summary"]["high_priority_parser_gap_count"] == 1
    gap = presentation["parsing_gaps"]["fields"][0]
    assert gap["field"] == "phone_hint"
    assert gap["priority"] == "high"
    assert gap["examples"][0]["value"] == "+353 *** 1234"
    assert "phone_hint" in render_text(presentation)
    assert "Parser-gap review" in render_html(presentation)


def test_reparse_uses_current_adapter_without_network_collection(tmp_path: Path) -> None:
    _write(
        tmp_path / "run.json",
        {"query": "johnsmith", "query_type": "username", "tools": ["maigret"]},
    )
    _write(
        tmp_path / "maigret" / "parsed.json",
        [
            {
                "site": "GitHub",
                "url": "https://github.com/johnsmith",
                "status": "claimed",
                "username": "johnsmith",
                "fullname": "John",
                "created_at": "2010-06-04T14:31:24Z",
                "uid": "296614",
            }
        ],
    )
    _write(tmp_path / "maigret" / "normalised.json", [])

    result = reparse_audit(tmp_path)
    presentation = build_audit_presentation(tmp_path)

    assert result["tools"]["maigret"]["state"] == "completed"
    assert (tmp_path / "maigret" / "normalised-current.json").is_file()
    assert presentation["summary"]["account_count"] == 1
    assert presentation["summary"]["timeline_event_count"] == 1
    assert presentation["accounts"][0]["profile"]["display_name"] == "John"


def test_presentation_keeps_same_username_separate_across_platforms(tmp_path: Path) -> None:
    _write(
        tmp_path / "run.json",
        {"query": "johnsmith", "query_type": "username", "tools": ["maigret"]},
    )
    _write(
        tmp_path / "maigret" / "summary.json",
        {"tool": "maigret", "state": "completed", "normalised_count": 2},
    )
    _write(
        tmp_path / "maigret" / "normalised.json",
        [
            _finding(
                tool="maigret",
                platform="GitHub",
                url="https://github.com/johnsmith",
                profile={"account_id": "johnsmith", "display_name": "John"},
            ),
            _finding(
                tool="maigret",
                platform="Pinterest",
                url="https://pinterest.com/johnsmith",
                profile={"account_id": "johnsmith", "display_name": "John Smith"},
            ),
        ],
    )

    presentation = build_audit_presentation(tmp_path)

    assert presentation["summary"]["account_count"] == 2
    assert {account["platform"] for account in presentation["accounts"]} == {
        "GitHub",
        "Pinterest",
    }
    username = presentation["entities"]["usernames"][0]
    assert username["account_count"] == 2
    assert username["platforms"] == ["GitHub", "Pinterest"]


def test_oversized_audit_json_is_not_loaded(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (collector_audit.MAX_AUDIT_JSON_BYTES + 1))

    sentinel = {"fallback": True}
    assert collector_audit._read_json(path, sentinel) is sentinel

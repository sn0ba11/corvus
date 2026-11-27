from typing import Any

import pytest

import app.adapters.builtin as builtin
from app.adapters.builtin import (
    InstaloaderAdapter,
    NexfilAdapter,
    SocialAnalyzerAdapter,
    _is_boilerplate_biography,
    _is_placeholder_avatar,
    _maigret_account_evidence_fields,
    _normalise,
    _normalise_timestamp,
    _profile_evidence,
    _timestamp_precision,
    platform_details,
)
from app.models.schemas import FindingStatus, QueryType


def details(record: dict[str, Any]) -> dict[str, str]:
    return {row["label"]: row["value"] for row in platform_details(record)}


def one(record: dict[str, Any]):
    return _normalise("maigret", [record], "example", QueryType.USERNAME, "raw")[0]


# 1. bare years
def test_bare_year_is_a_year() -> None:
    assert _normalise_timestamp("2005") == "2005-01-01T00:00:00+00:00"
    assert _normalise_timestamp(2005) == "2005-01-01T00:00:00+00:00"
    assert _timestamp_precision("2005") == "year"
    assert details({"created_at": "2005"})["Account created"] == "2005"


def test_small_numbers_are_not_timestamps() -> None:
    assert _normalise_timestamp("1234567") is None
    assert _normalise_timestamp("1500") is None
    assert details({"created_at": "1234567"})["Account created"] == "1234567"


def test_ten_digit_epoch_still_a_date() -> None:
    assert _normalise_timestamp("1741000755") == "2025-03-03T11:19:15+00:00"
    assert details({"created_at": 1741000755})["Account created"] == "2025-03-03"


# 2. date display
def test_pinterest_and_pastebin_dates() -> None:
    assert _normalise_timestamp("Wed, 15 Aug 2012 09:21:06 +0000") == "2012-08-15T09:21:06+00:00"
    assert _normalise_timestamp("Monday 5th of September 2011 05:11:58 AM CDT") == (
        "2011-09-05T10:11:58+00:00"
    )
    assert details({"created_at": "Wed, 15 Aug 2012 09:21:06 +0000"})["Account created"] == "2012-08-15 09:21 UTC"
    assert (
        details({"last_seen": "Monday 5th of September 2011 05:11:58 AM CDT"})["Last activity"]
        == "2011-09-05 10:11 UTC"
    )


def test_date_only_and_midnight_display_as_dates() -> None:
    assert details({"created_at": "2013-10-30"})["Account created"] == "2013-10-30"
    assert details({"last_modified": "2013-10-30T00:00:00Z"})["Profile record modified"] == "2013-10-30"
    assert details({"created_at": "2013-10-30 19:05:41 UTC"})["Account created"] == "2013-10-30 19:05 UTC"


def test_unparseable_date_shows_raw_and_metadata_keeps_raw() -> None:
    assert details({"created_at": "a long time ago"})["Account created"] == "a long time ago"
    finding = one({"site": "X", "url": "https://x.example/u", "status": "claimed", "created_at": "2005"})
    assert finding.metadata["created_at"] == "2005"


# 3. placeholder avatars
@pytest.mark.parametrize(
    "url",
    [
        "https://abs.twimg.com/sticky/default_profile_images/default_profile.png",
        "https://pastebin.com/img/guest.png",
        "https://simg-ssl.duolingo.com/avatar/default_2",
        "https://assets.leetcode.com/users/default_avatar.jpg",
        "https://cdn.example/images/default.png",
    ],
)
def test_placeholder_avatars_detected(url: str) -> None:
    assert _is_placeholder_avatar(url)


def test_real_avatars_and_gravatar_identicons_are_kept() -> None:
    assert not _is_placeholder_avatar("https://cdn.example/avatars/12345.jpg")
    assert not _is_placeholder_avatar("https://www.gravatar.com/avatar/abc123?d=identicon")
    assert _profile_evidence({"avatar_url": "https://cdn.example/a.jpg"}).avatar_url == "https://cdn.example/a.jpg"


def test_placeholder_avatar_not_stored_but_retained_in_metadata() -> None:
    url = "https://pastebin.com/img/guest.png"
    finding = one({"site": "Pastebin", "url": "https://pastebin.com/u/x", "status": "claimed", "image": url})
    assert finding.profile.avatar_url is None
    assert finding.metadata["avatar_placeholder_url"] == url


def test_placeholder_avatar_is_not_account_evidence() -> None:
    assert _maigret_account_evidence_fields({"image": "https://x.example/img/guest.png"}) == ()
    assert _maigret_account_evidence_fields({"image": "https://x.example/u/1.jpg"}) == ("image",)


# 4. boilerplate biographies
def test_boilerplate_biography_detection() -> None:
    assert _is_boilerplate_biography("NASA is on Snapchat! (@nasa) | 751.6k subscribers")
    assert _is_boilerplate_biography("Patrick Torvalds is on Facebook. Join Facebook to connect")
    assert _is_boilerplate_biography("Hello. Join Facebook to connect with friends")
    assert not _is_boilerplate_biography("I write kernels and live in Portland.")


def test_boilerplate_biography_dropped_and_kept_as_page_description() -> None:
    text = "NASA is on Snapchat! (@nasa) | 751.6k subscribers"
    finding = one({"site": "Snapchat", "url": "https://snapchat.com/add/nasa", "status": "claimed", "description": text})
    assert finding.profile.biography is None
    assert finding.metadata["page_description"] == text


def test_boilerplate_falls_through_to_real_biography() -> None:
    profile = _profile_evidence({"bio": "Patrick is on Facebook. Join Facebook to connect", "about": "Kernel hacker"})
    assert profile.biography == "Kernel hacker"
    assert _profile_evidence({"bio": "Real bio"}).biography == "Real bio"


# 5. Freelancer
def test_freelancer_api_url_replaced() -> None:
    api = "https://www.freelancer.com/api/users/0.1/users?compact=true&usernames%5B%5D=example"
    finding = one({"site": "Freelancer.com", "url": api, "status": "claimed", "username": "example"})
    assert finding.display_url == "https://www.freelancer.com/u/example"
    assert finding.metadata["evidence_url"] == api
    other = one({"site": "Freelancer.com", "url": api, "status": "claimed"})
    assert other.display_url == "https://www.freelancer.com/u/example"


# 6. country codes
def test_bare_country_code_is_not_a_location() -> None:
    record = {"site": "DailyMotion", "url": "https://d.example/x", "status": "claimed", "country": "GI"}
    finding = one(record)
    assert finding.profile.location is None
    assert details(record)["Profile country"] == "GI"
    assert details({"country_code": "US"})["Country code"] == "US"
    assert _profile_evidence({"country_code": "US"}).location is None


def test_real_location_kept_and_code_skipped_in_chain() -> None:
    assert _profile_evidence({"location": "London"}).location == "London"
    assert _profile_evidence({"country": "GI", "city": "Gibraltar"}).location == "Gibraltar"


# 7. PRS-01
def make(i: int) -> dict[str, Any]:
    return {"site": f"Site{i}", "url": f"https://site{i}.example/u", "status": "claimed"}


def test_valid_invalid_valid_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    real = builtin.canonicalise_url

    def fake(url: str) -> str:
        if "site1" in url:
            raise ValueError("boom " + "x" * 400)
        return real(url)

    monkeypatch.setattr(builtin, "canonicalise_url", fake)
    findings = _normalise("maigret", [make(0), make(1), make(2)], "example", QueryType.USERNAME, "raw")
    assert len(findings) == 3
    assert findings[0].display_url == "https://site0.example/u"
    assert findings[2].display_url == "https://site2.example/u"
    bad = findings[1]
    assert bad.finding_type == "unparsed_record"
    assert bad.status == FindingStatus.ERROR
    assert bad.confidence == 0.0
    assert bad.platform == "Site1"
    assert bad.value == "example"
    assert bad.display_url is None and bad.canonical_url is None
    assert bad.source_record_id == "1"
    assert bad.source_tool == "maigret" and bad.raw_output_reference == "raw"
    assert bad.metadata["raw_record"] == make(1)
    assert len(bad.metadata["parse_error"]) <= 300


def test_all_invalid_batch_is_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str) -> str:
        raise ValueError("bad")

    monkeypatch.setattr(builtin, "canonicalise_url", fake)
    findings = _normalise("maigret", [make(0), make(1)], "example", QueryType.USERNAME, "raw")
    assert [f.finding_type for f in findings] == ["unparsed_record"] * 2
    assert all(f.status == FindingStatus.ERROR for f in findings)


def test_oversized_raw_record_is_stored_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str) -> str:
        raise ValueError("bad")

    monkeypatch.setattr(builtin, "canonicalise_url", fake)
    record = {**make(0), "blob": "y" * 6000}
    bad = _normalise("maigret", [record], "example", QueryType.USERNAME, None)[0]
    assert isinstance(bad.metadata["raw_record"], str)
    assert len(bad.metadata["raw_record"]) == 4000


def test_dict_display_name_is_rejected_field_not_error() -> None:
    finding = one({**make(0), "display_name": {"given": "Example"}})
    assert finding.finding_type == "profile"
    assert finding.profile.display_name is None
    assert finding.metadata["rejected_fields"] == [
        {"field": "display_name", "reason": "expected text, got dict"}
    ]
    assert "rejected_fields" not in one(make(0)).metadata


def test_numbers_coerced_to_text_and_bool_rejected() -> None:
    profile = _profile_evidence({"display_name": 42, "id": 7.5, "bio": True})
    assert profile.display_name == "42"
    assert profile.account_id == "7.5"
    assert profile.biography is None


@pytest.mark.parametrize("adapter_class", [NexfilAdapter, SocialAnalyzerAdapter, InstaloaderAdapter])
def test_post_processing_keeps_unparsed_records(
    adapter_class: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = builtin.canonicalise_url

    def fake(url: str) -> str:
        if "site1" in url:
            raise ValueError("bad")
        return real(url)

    monkeypatch.setattr(builtin, "canonicalise_url", fake)
    findings = adapter_class().normalise_results([make(0), make(1)], "example", QueryType.USERNAME, "raw")
    assert findings[0].finding_type != "unparsed_record"
    assert findings[1].finding_type == "unparsed_record"
    assert findings[1].status == FindingStatus.ERROR
    assert findings[1].confidence == 0.0


def test_medium_feed_time_is_not_reported_as_account_activity() -> None:
    from app.adapters.builtin import platform_details

    rows = {
        row["label"]: row["value"]
        for row in platform_details(
            {
                "site": "Medium",
                "_extractor": "Medium RSS",
                "latest_activity_at": "2026-11-13 13:41:45 UTC",
            }
        )
    }

    assert "Last activity" not in rows
    assert rows["Feed generated"] == "2026-11-13 13:41 UTC"
    api_rows = {
        row["label"]
        for row in platform_details(
            {"_extractor": "Chess.com API", "latest_activity_at": "2026-08-02 11:02:47 UTC"}
        )
    }
    assert "Last activity" in api_rows

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.db import Base, Finding, Investigation, LocationObservation, ToolRun
from app.services.browser_agent import BrowserCaptureResult
from app.services.investigation_accounts import build_investigation_account_view
from app.services.managed_browser_extraction import (
    collector_for_url,
    import_location_history,
    import_managed_search_results,
    import_social_profile,
    normalise_social_profile,
    policy_for_url,
    search_results_from_capture,
)


def capture(**overrides):  # type: ignore[no-untyped-def]
    values = {
        "status": "completed",
        "requested_url": "https://www.tiktok.com/@johnsmith",
        "final_url": "https://www.tiktok.com/@johnsmith",
        "observed_at": datetime(2026, 8, 6, tzinfo=UTC),
        "page_title": "John Smith (@johnsmith) | TikTok",
        "challenge_type": None,
        "screenshot_reference": "case/job/page.png",
        "html_reference": None,
        "manifest_reference": "case/job/manifest.json",
        "screenshot_sha256": "1" * 64,
        "html_sha256": None,
        "manifest_sha256": "2" * 64,
        "browser_version": "140.0",
        "extraction_kind": "social_profile",
        "extraction_platform": "tiktok",
        "authenticated_session": False,
        "extracted": {
            "canonical_url": "https://www.tiktok.com/@johnsmith",
            "title": "John Smith",
            "meta": {"og:image": "https://cdn.example/avatar.jpg"},
            "embedded_state": {
                "user": {
                    "uniqueId": "johnsmith",
                    "nickname": "John Smith",
                    "id": "7372405988308567046",
                    "secUid": "MS4wLjABExample",
                    "verified": False,
                    "privateAccount": False,
                    "followerCount": 42,
                }
            },
        },
    }
    values.update(overrides)
    return BrowserCaptureResult(**values)


def database() -> tuple[Session, Investigation]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    investigation = Investigation(
        title="johnsmith",
        query="johnsmith",
        query_type="username",
        selected_tools=[],
        tags=[],
    )
    db.add(investigation)
    db.commit()
    db.refresh(investigation)
    return db, investigation


def test_platform_request_policies_are_path_bounded() -> None:
    assert collector_for_url("https://www.tiktok.com/@johnsmith") == "browser_tiktok"
    assert collector_for_url("https://www.tiktok.com/@johnsmith/video/123") == "generic_capture"
    assert collector_for_url("https://www.instagram.com/johnsmith/") == "browser_instagram"
    assert collector_for_url("https://www.instagram.com/p/example/") == "generic_capture"
    assert collector_for_url("https://www.facebook.com/johnsmith") == "browser_facebook"
    assert collector_for_url("https://www.facebook.com/groups/example") == "generic_capture"
    assert collector_for_url("https://www.facebook.com/profile.php?id=123") == "browser_facebook"
    assert collector_for_url("https://www.strava.com/athletes/123") == "browser_strava"
    assert collector_for_url("https://www.strava.com/settings/profile") == "generic_capture"
    assert (
        policy_for_url("https://connect.garmin.com/modern/profile/abc").platform == "Garmin Connect"
    )  # type: ignore[union-attr]
    assert (
        policy_for_url("https://connect.garmin.com/app/profile/abc").platform == "Garmin Connect"
    )  # type: ignore[union-attr]


def test_pinterest_threads_profile_capture_is_path_bounded() -> None:
    assert collector_for_url('https://www.pinterest.com/lazeredwithlove/') == 'browser_pinterest'
    assert collector_for_url('https://www.threads.com/@lazeredwithlove') == 'browser_threads'
    assert collector_for_url('https://www.threads.net/@lazeredwithlove') == 'browser_threads'
    assert collector_for_url('https://www.pinterest.com/pin/12345/') == 'generic_capture'
    assert collector_for_url('https://www.threads.com/@lazeredwithlove/post/12345') == 'generic_capture'
    url, handle, profile, _ = normalise_social_profile(
        'pinterest', 'https://www.pinterest.com/lazeredwithlove/',
        {'canonical_url': 'https://www.pinterest.com/lazeredwithlove/',
         'meta': {'og:title': 'Lazered With Love - Pinterest', 'og:description': 'Irish decorations',
                  'og:image': 'https://cdn.example/avatar.jpg'}}
    )
    assert handle == 'lazeredwithlove'
    assert profile['avatar_url'] == 'https://cdn.example/avatar.jpg'
    assert url == 'https://www.pinterest.com/lazeredwithlove'


def test_social_profile_import_creates_source_backed_finding() -> None:
    db, investigation = database()
    assert import_social_profile(db, investigation, capture(), "browser_tiktok") == 1
    db.commit()
    finding = db.scalar(select(Finding))
    assert finding is not None
    assert finding.platform == "TikTok"
    assert finding.value == "johnsmith"
    assert finding.extra["profile"]["account_id"] == "7372405988308567046"
    assert finding.extra["profile"]["followers"] == 42
    assert finding.extra["authenticated_observation"] is False
    assert finding.source_tool == "browser_tiktok"
    account_view = build_investigation_account_view(investigation)
    assert account_view["accounts"][0]["profile"]["display_name"] == "John Smith"


def test_tiktok_parser_prefers_exact_profile_identity_over_unrelated_ids() -> None:
    url, username, profile, details = normalise_social_profile(
        "tiktok",
        "https://www.tiktok.com/@johnsmith",
        {
            "canonical_url": "https://www.tiktok.com/@johnsmith",
            "embedded_state": {
                "tracking": {"id": "wrong-id", "name": "Generic page"},
                "userInfo": {
                    "user": {
                        "uniqueId": "johnsmith",
                        "nickname": "John Smith",
                        "id": "123456",
                        "secUid": "secure-example",
                        "signature": "Public biography",
                    },
                    "stats": {"followerCount": "1.2K", "followingCount": 30, "heartCount": 88},
                },
            },
        },
    )
    assert url == "https://www.tiktok.com/@johnsmith"
    assert username == "johnsmith"
    assert profile["account_id"] == "123456"
    assert profile["display_name"] == "John Smith"
    assert profile["followers"] == 1200
    assert {item["label"]: item["value"] for item in details} == {
        "Secure user ID": "secure-example",
        "Likes": "88",
    }


def test_instagram_meta_parser_extracts_identity_and_counts() -> None:
    _, username, profile, _ = normalise_social_profile(
        "instagram",
        "https://www.instagram.com/example/",
        {
            "canonical_url": "https://www.instagram.com/example/",
            "meta": {
                "og:title": "Example Person (@example) • Instagram photos and videos",
                "og:description": "1.5K Followers, 42 Following, 77 Posts - See Instagram photos and videos from Example Person (@example)",
                "og:image": "https://cdn.example/avatar.jpg",
            },
        },
    )
    assert username == "example"
    assert profile["display_name"] == "Example Person"
    assert profile["followers"] == 1500
    assert profile["following"] == 42
    assert profile["posts"] == 77
    assert profile["avatar_url"] == "https://cdn.example/avatar.jpg"
    assert "biography" not in profile


def test_existing_profile_is_enriched_without_losing_prior_profile() -> None:
    db, investigation = database()
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
        query_value="johnsmith",
        query_type="username",
    )
    db.add(run)
    db.flush()
    existing = Finding(
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="johnsmith",
        finding_type="profile",
        platform="TikTok",
        value="johnsmith",
        canonical_url="https://www.tiktok.com/@johnsmith",
        display_url="https://www.tiktok.com/@johnsmith",
        status="likely",
        confidence=0.65,
        extra={"profile": {"display_name": "John Smith", "following": 12}},
        source_tool="maigret",
        source_tools=["maigret"],
    )
    db.add(existing)
    db.commit()
    assert import_social_profile(db, investigation, capture(), "browser_tiktok") == 0
    db.commit()
    db.refresh(existing)
    assert existing.status == "found"
    assert existing.confidence == 0.95
    assert existing.extra["profile"]["display_name"] == "John Smith"
    assert existing.extra["profile"]["following"] == 12
    assert existing.extra["profile"]["followers"] == 42
    assert existing.source_tools == ["browser_tiktok", "maigret"]
    assert existing.extra["source_metadata"]["browser_tiktok"]["managed_browser"] is True


def test_managed_location_html_imports_structured_observation(tmp_path: Path) -> None:
    db, investigation = database()
    evidence = tmp_path / "case" / "job"
    evidence.mkdir(parents=True)
    (evidence / "page.html").write_text(
        """
        <html><head><script type="application/ld+json">
        {"@type":"ExerciseAction","location":{"@type":"Place","name":"Phoenix Park","address":{"addressLocality":"Dublin","addressCountry":"IE"}},"startTime":"2026-08-01T09:00:00Z"}
        </script></head><body>Privacy zone applied</body></html>
        """,
        encoding="utf-8",
    )
    result = capture(
        requested_url="https://www.strava.com/activities/123",
        final_url="https://www.strava.com/activities/123",
        html_reference="case/job/page.html",
        extraction_kind="location_history",
        extraction_platform="strava",
        extracted={"platform": "strava"},
    )
    assert import_location_history(db, investigation, result, tmp_path) == 1
    db.commit()
    observation = db.scalar(select(LocationObservation))
    assert observation is not None
    assert observation.place_name == "Phoenix Park"
    assert observation.source_platform == "Strava"
    assert observation.raw_evidence["managed_browser"] is True


def test_managed_location_import_falls_back_to_extracted_jsonld(tmp_path: Path) -> None:
    db, investigation = database()
    result = capture(
        requested_url="https://www.strava.com/activities/456",
        final_url="https://www.strava.com/activities/456",
        html_reference=None,
        extraction_kind="location_history",
        extraction_platform="strava",
        extracted={
            "meta": {},
            "jsonld": [
                {
                    "@type": "ExerciseAction",
                    "location": {"@type": "Place", "name": "Howth Head"},
                    "startTime": "2026-08-02T07:30:00Z",
                }
            ],
            "visible_text": "Start and end hidden by privacy zone",
        },
    )
    assert import_location_history(db, investigation, result, tmp_path) == 1
    db.commit()
    observation = db.scalar(select(LocationObservation))
    assert observation is not None
    assert observation.place_name == "Howth Head"
    assert observation.privacy_masked is True
    assert observation.precision_radius_m >= 1000


def test_managed_location_import_retains_capture_without_inventing_location(
    tmp_path: Path,
) -> None:
    db, investigation = database()
    result = capture(
        requested_url="https://www.strava.com/athletes/123",
        final_url="https://www.strava.com/athletes/123",
        html_reference=None,
        extraction_kind="location_history",
        extraction_platform="strava",
        extracted={"meta": {"og:title": "Example athlete"}, "jsonld": []},
    )
    assert import_location_history(db, investigation, result, tmp_path) == 0
    assert db.scalar(select(LocationObservation)) is None


def test_search_policies_route_to_distinct_platforms() -> None:
    assert collector_for_url("https://www.google.com/maps/place/x") == "browser_google_maps"
    assert collector_for_url("https://www.google.com/search?q=someone") == "browser_google_search"
    assert collector_for_url("https://www.google.ie/search?q=someone") == "browser_google_search"
    assert collector_for_url("https://www.bing.com/search?q=someone") == "browser_bing_search"
    assert collector_for_url("https://duckduckgo.com/?q=someone") == "browser_duckduckgo_search"
    assert collector_for_url("https://search.brave.com/search?q=someone") == "browser_brave_search"
    assert collector_for_url("https://www.qwant.com/?q=someone&t=web") == "browser_qwant_search"
    assert collector_for_url("https://www.startpage.com/sp/search?query=someone") == "browser_startpage_search"
    policy = policy_for_url("https://www.google.com/search?q=someone")
    assert policy is not None and policy.kind == "search_results"


def test_search_results_from_capture_filters_chrome_links_and_unwraps_redirect() -> None:
    result = capture(
        requested_url="https://www.google.com/search?q=someone",
        final_url="https://www.google.com/search?q=someone",
        extraction_kind="search_results",
        extraction_platform="google_search",
        extracted={
            "links": [
                {"href": "https://www.google.com/search?q=someone&tbm=isch", "text": "Images"},
                {
                    "href": "https://www.google.com/url?q=https://example.org/profile&sa=U",
                    "text": "Example profile",
                },
                {"href": "https://example.org/profile", "text": "Example profile duplicate"},
                {
                    "href": "https://webcache.googleusercontent.com/search?q=cache:x",
                    "text": "Cached",
                },
                {"href": "http://127.0.0.1/admin", "text": "Should be rejected"},
                {"href": "not a url", "text": "Should be rejected too"},
            ]
        },
    )

    items, rejected = search_results_from_capture(result)

    assert [item.url for item in items] == ["https://example.org/profile"]
    assert items[0].title == "Example profile"
    assert items[0].snippet is None
    assert rejected == 2


def test_search_results_from_capture_prefers_structured_serp_cards_with_snippets() -> None:
    result = capture(
        requested_url="https://www.google.com/search?q=someone",
        final_url="https://www.google.com/search?q=someone",
        extraction_kind="search_results",
        extraction_platform="google_search",
        extracted={
            "search_results": [
                {
                    "href": "https://example.org/profile",
                    "title": "Example profile",
                    "snippet": "Useful search-engine snippet with identity context.",
                    "rank": 1,
                }
            ],
            "links": [
                {"href": "https://noise.example/unrelated", "text": "Navigation noise"}
            ],
        },
    )

    items, rejected = search_results_from_capture(result)

    assert rejected == 0
    assert len(items) == 1
    assert items[0].url == "https://example.org/profile"
    assert items[0].title == "Example profile"
    assert items[0].snippet == "Useful search-engine snippet with identity context."


def test_search_results_from_capture_caps_at_max_import_results() -> None:
    links = [
        {"href": f"https://example.org/result-{index}", "text": f"Result {index}"}
        for index in range(80)
    ]
    result = capture(
        requested_url="https://www.bing.com/search?q=someone",
        final_url="https://www.bing.com/search?q=someone",
        extraction_kind="search_results",
        extraction_platform="bing_search",
        extracted={"links": links},
    )

    items, _ = search_results_from_capture(result)

    assert len(items) == 50


def test_username_managed_search_rejects_irrelevant_and_wrong_site_results() -> None:
    db, investigation = database()
    investigation.query = "corvus_fixture_nq7az"
    investigation.query_type = "username"
    result = capture(
        requested_url="https://www.bing.com/search?q=%22corvus_fixture_nq7az%22+site%3Ainstagram.com&cc=ie",
        final_url="https://www.bing.com/search?q=%22corvus_fixture_nq7az%22+site%3Ainstagram.com&cc=ie",
        extraction_kind="search_results",
        extraction_platform="bing_search",
        extracted={
            "search_results": [
                {
                    "href": "https://www.action.com/nl-nl/",
                    "title": "Action Nederland",
                    "snippet": "Unrelated shopping result",
                },
                {
                    "href": "https://www.instagram.com/corvus_fixture_nq7az/",
                    "title": "corvus_fixture_nq7az • Instagram photos and videos",
                    "snippet": "Instagram profile",
                },
            ]
        },
    )
    assert import_managed_search_results(db, investigation, result) == 1
    db.commit()
    findings = list(db.scalars(select(Finding)))
    assert [item.canonical_url for item in findings] == [
        "https://www.instagram.com/corvus_fixture_nq7az"
    ]


def test_name_managed_search_rejects_unrelated_rendered_results() -> None:
    db, investigation = database()
    investigation.query = "Linus Torvalds"
    investigation.query_type = "name"
    result = capture(
        requested_url="https://www.bing.com/search?q=%22Linus+Torvalds%22&cc=ie",
        final_url="https://www.bing.com/search?q=%22Linus+Torvalds%22&cc=ie",
        extraction_kind="search_results",
        extraction_platform="bing_search",
        extracted={
            "search_results": [
                {
                    "href": "https://forums.example.org/peoples-pension",
                    "title": "People's pension advice",
                    "snippet": "Completely unrelated search result",
                },
                {
                    "href": "https://en.wikipedia.org/wiki/Linus_Torvalds",
                    "title": "Linus Torvalds - Wikipedia",
                    "snippet": "Finnish software engineer and creator of Linux",
                },
            ]
        },
    )

    assert import_managed_search_results(db, investigation, result) == 1
    db.commit()
    findings = list(db.scalars(select(Finding)))
    assert [item.canonical_url for item in findings] == [
        "https://en.wikipedia.org/wiki/Linus_Torvalds"
    ]


def test_import_managed_search_results_creates_findings() -> None:
    db, investigation = database()
    result = capture(
        requested_url="https://www.google.com/search?q=johnsmith+example",
        final_url="https://www.google.com/search?q=johnsmith+example",
        extraction_kind="search_results",
        extraction_platform="google_search",
        extracted={
            "links": [{"href": "https://example.org/johnsmith", "text": "John Smith profile"}]
        },
    )

    assert import_managed_search_results(db, investigation, result) == 1
    db.commit()
    finding = db.scalar(select(Finding))
    assert finding is not None
    assert finding.canonical_url == "https://example.org/johnsmith"
    assert finding.title == "John Smith profile"
    assert finding.source_tool == "search_browser_capture"
    assert finding.extra["search_browser_capture"]["engine"] == "google"
    assert finding.extra["search_browser_capture"]["query"] == "johnsmith example"


def test_import_managed_search_results_skips_non_search_capture_url() -> None:
    db, investigation = database()
    result = capture(extraction_kind="social_profile")

    assert import_managed_search_results(db, investigation, result) == 0


def test_profile_objects_survive_page_state_larger_than_the_walk_limit() -> None:
    bulky_state = {f"section-{index}": {"values": list(range(60))} for index in range(120)}
    payload = {
        "url": "https://www.tiktok.com/@example_user",
        "meta": {},
        "embedded_state": bulky_state,
        "profile_objects": [
            {"uniqueId": "example_user", "nickname": "Example Person", "signature": "Bio text",
             "followerCount": 321}
        ],
    }
    _, username, profile, _ = normalise_social_profile(
        "tiktok", "https://www.tiktok.com/@example_user", payload
    )
    assert username == "example_user"
    assert profile["display_name"] == "Example Person"
    assert profile["followers"] == 321



def test_an_absent_account_page_is_not_read_as_a_profile() -> None:
    """Shapes captured live on 2026-11-13: TikTok serves these with HTTP 200."""
    absent = {
        "url": "https://www.tiktok.com/@unused_handle",
        "title": "Couldn’t find this account. Visit TikTok to discover more trending creators, hashtags, and sounds.",
        "meta": {"og:title": "Couldn’t find this account. Visit TikTok to discover more trending creators"},
        "profile_objects": [{"id": "1", "name": "Some unrelated page object"}],
    }
    _, username, profile, details = normalise_social_profile(
        "tiktok", "https://www.tiktok.com/@unused_handle", absent
    )
    assert username == "unused_handle" and profile == {} and details == []
    # A real profile whose page title is only the site's own name still gets its name
    # from the profile data, never from that title.
    real = {
        "url": "https://www.tiktok.com/@example_user", "title": "TikTok - Make Your Day", "meta": {},
        "profile_objects": [{"uniqueId": "example_user", "nickname": "Example Person", "followerCount": 9}],
    }
    _, _, profile, _ = normalise_social_profile("tiktok", "https://www.tiktok.com/@example_user", real)
    assert profile["display_name"] == "Example Person"
    generic_only = {"url": "https://www.tiktok.com/@example_user", "title": "TikTok - Make Your Day", "meta": {}}
    _, _, profile, _ = normalise_social_profile("tiktok", "https://www.tiktok.com/@example_user", generic_only)
    assert not profile.get("display_name")


def test_a_site_name_title_or_not_found_body_is_not_a_profile() -> None:
    """Snapchat's page for an unused handle on 2026-11-13: HTTP 404, title "Snapchat"."""
    absent = {
        "url": "https://www.snapchat.com/@unused_handle", "title": "Snapchat", "meta": {},
        "visible_text": "Stories Spotlight Chat Lenses Log In Sorry, This content was not found Company",
        "profile_objects": [{"id": "x"}],
    }
    _, _, profile, _ = normalise_social_profile("snapchat", "https://www.snapchat.com/@unused_handle", absent)
    assert profile == {}
    # Even without the body text, the site's own name is never taken as a display name.
    _, _, profile, _ = normalise_social_profile(
        "snapchat", "https://www.snapchat.com/@unused_handle", {**absent, "visible_text": ""}
    )
    assert not profile.get("display_name")

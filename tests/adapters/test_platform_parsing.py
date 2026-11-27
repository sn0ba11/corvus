import pytest

from app.adapters.builtin import BlackbirdAdapter, MaigretAdapter, SocialAnalyzerAdapter
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.collector_audit import analyse_parsing_gaps, extract_entities, merge_accounts


def details_for(finding: FindingRecord) -> dict[str, str]:
    return {item["label"]: item["value"] for item in finding.metadata["platform_details"]}


def test_blackbird_contact_details_reach_account_and_entity_views() -> None:
    metadata = [
        {"name": "Name", "value": "Example Person"},
        {"name": "Email", "value": "e***@example.test"},
        {"type": "Phone number", "value": "+353 *** 1234"},
        {"name": "Website", "value": "https://example.test/about"},
        {"name": "Company", "value": "Example Studio"},
        {"name": "Occupation", "value": "Designer"},
        {"name": "Previous username", "value": "older_example"},
    ]
    finding = BlackbirdAdapter().normalise_results(
        [{"name": "Example", "url": "https://example.test/example",
          "status": "FOUND", "metadata": metadata}],
        "example", QueryType.USERNAME, "raw",
    )[0]
    record = finding.model_dump(mode="json")
    accounts = merge_accounts([record])
    details = {item["label"]: item["value"] for item in accounts[0]["details"]}
    assert details["Reported email"] == "e***@example.test"
    assert details["Reported phone"] == "+353 *** 1234"
    assert details["Website"] == "https://example.test/about"
    assert details["Company / employer"] == "Example Studio"
    assert details["Role"] == "Designer"
    assert extract_entities(accounts)["emails"][0]["value"] == "e***@example.test"
    assert finding.metadata["metadata"] == metadata
    gaps = analyse_parsing_gaps([record])["fields"]
    assert [gap["field"] for gap in gaps] == ["metadata[6].Previous username"]
    assert gaps[0]["examples"][0]["value"] == "older_example"


def test_blackbird_contact_details_do_not_confirm_a_bare_claim() -> None:
    finding = BlackbirdAdapter().normalise_results(
        [{"name": "Example", "url": "https://example.test/example", "status": "FOUND",
          "metadata": [{"name": "Email", "value": "e***@example.test"},
                       {"name": "Website", "value": {"unexpected": "object"}},
                       {"name": "Phone", "value": False}]}],
        "example", QueryType.USERNAME, "raw",
    )[0]
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.confidence == 0.10
    assert details_for(finding)["Reported email"] == "e***@example.test"
    assert "Website" not in details_for(finding)
    assert "Reported phone" not in details_for(finding)


def _blackbird(metadata: list[dict[str, object]], status: str = "FOUND") -> FindingRecord:
    return BlackbirdAdapter().normalise_results(
        [{"name": "Example", "url": "https://example.test/example", "status": status,
          "metadata": metadata}],
        "example", QueryType.USERNAME, "raw",
    )[0]


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "Avatar", "value": "not-a-url"},
        {"name": "Avatar", "value": "https://example.test/img/default.png"},
        {"name": "Followers", "value": "lots"},
        {"name": "Verified", "value": "maybe"},
        {"name": "Joined", "value": "yesterday-ish"},
        {"name": "Name", "value": "N/A"},
        {"name": "Name", "value": "  "},
        {"name": "Name", "value": "Example profile"},
        {"name": "Bio", "value": "--"},
    ],
)
def test_blackbird_invalid_metadata_does_not_upgrade_a_bare_claim(entry: dict[str, object]) -> None:
    finding = _blackbird([entry])
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.confidence == 0.10
    assert finding.metadata["blackbird_unverified_claim"] is True
    assert finding.profile.avatar_url is None
    assert finding.profile.followers is None


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "Avatar", "value": "https://example.test/a.png"},
        {"name": "Followers", "value": "1,204"},
        {"name": "Name", "value": "Real Person"},
    ],
)
def test_blackbird_valid_metadata_still_upgrades_a_claim(entry: dict[str, object]) -> None:
    finding = _blackbird([entry])
    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.80


def test_blackbird_not_found_is_unaffected_by_metadata() -> None:
    finding = _blackbird([{"name": "Name", "value": "Real Person"}], status="NOT FOUND")
    assert finding.status != FindingStatus.FOUND


def test_maigret_platform_specific_fields_and_timeline_are_promoted() -> None:
    adapter = MaigretAdapter()
    findings = adapter.normalise_results(
        [
            {
                "site": "TikTok",
                "url": "https://www.tiktok.com/@example",
                "status": "claimed",
                "username": "example",
                "fullname": "Example Person",
                "tiktok_id": "7372405988308567046",
                "sec_uid": "MS4wLjABExample",
                "video_count": "3",
                "heart_count": "125",
                "digg_count": "7",
                "is_secret": "False",
            },
            {
                "site": "YouTube",
                "url": "https://www.youtube.com/@example/about",
                "channel_url": "https://www.youtube.com/@example",
                "status": "claimed",
                "username": "example",
                "youtube_channel_id": "UCexample",
                "is_family_safe": "True",
                "keywords": "example channel",
            },
            {
                "site": "StackOverflow",
                "url": "https://stackoverflow.com/users/filter?search=example",
                "link": "https://stackoverflow.com/users/1549900/example",
                "status": "claimed",
                "username": "example",
                "account_id": "1686788",
                "uid": "1549900",
                "created_at": "1343164674",
                "reputation": "42",
            },
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )

    tiktok = details_for(findings[0])
    assert findings[0].profile.account_id == "example"
    assert tiktok["Account ID"] == "7372405988308567046"
    assert tiktok["Secondary ID"] == "MS4wLjABExample"
    assert tiktok["Videos"] == "3"
    assert tiktok["Likes / favourites"] == "125"
    assert tiktok["Diggs"] == "7"
    assert tiktok["Private account"] == "No"

    youtube = details_for(findings[1])
    assert findings[1].canonical_url == "https://www.youtube.com/@example"
    assert findings[1].profile.account_id == "example"
    assert youtube["Account ID"] == "UCexample"
    assert youtube["Family-safe"] == "Yes"
    assert youtube["Keywords"] == "example channel"

    stack = details_for(findings[2])
    assert findings[2].canonical_url == "https://stackoverflow.com/users/1549900/example"
    assert findings[2].metadata["evidence_url"].startswith(
        "https://stackoverflow.com/users/filter"
    )
    assert findings[2].profile.account_id == "example"
    assert stack["Account ID"] == "1549900"
    assert stack["Network account ID"] == "1686788"
    assert stack["Reputation"] == "42"
    assert findings[2].metadata["timeline_events"][0]["timestamp"] == (
        "2012-07-24T21:17:54+00:00"
    )


def test_twitch_and_soundcloud_dates_and_metrics_are_semantically_promoted() -> None:
    adapter = MaigretAdapter()
    findings = adapter.normalise_results(
        [
            {
                "site": "Twitch",
                "url": "https://twitchtracker.com/example",
                "status": "claimed",
                "twitch_channel_id": "171311037",
                "twitch_username": "example",
                "created_at": "2017-08-25 01:54:28",
                "tracker_updated_at": "2026-09-02 09:34:23",
                "_extractor": "TwitchTracker",
            },
            {
                "site": "SoundCloud",
                "url": "https://soundcloud.com/example",
                "status": "claimed",
                "id": 25777195,
                "username": "example",
                "full_name": "Example Artist",
                "followers_count": 12,
                "followings_count": 6,
                "description": "Public SoundCloud biography",
                "city": "Dublin",
                "country_code": "IE",
                "created_at": "2014-04-10T12:00:00Z",
                "last_modified": "2017-04-14T14:12:05Z",
                "likes_count": 4,
                "track_count": 2,
                "playlist_count": 1,
                "reposts_count": 3,
                "comments_count": 5,
                "_extractor": "SoundCloud public hydration",
            },
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )

    twitch = findings[0]
    twitch_details = details_for(twitch)
    assert twitch.profile.account_id == "example"
    assert twitch_details["Account ID"] == "171311037"
    assert twitch_details["Account created"] == "2017-08-25 01:54 UTC"
    assert twitch_details["Source data updated"] == "2026-09-02 09:34:23"
    assert twitch.metadata["timeline_events"][0]["label"] == "Account registered"
    assert twitch.metadata["timeline_events"][1]["label"] == "TwitchTracker updated"
    assert twitch.metadata["timeline_events"][1]["include_in_timeline"] is False

    soundcloud = findings[1]
    soundcloud_details = details_for(soundcloud)
    assert soundcloud.profile.display_name == "Example Artist"
    assert soundcloud.profile.location == "Dublin, IE"
    assert soundcloud.profile.followers == 12
    assert soundcloud.profile.following == 6
    assert soundcloud.profile.biography == "Public SoundCloud biography"
    assert soundcloud_details["Account ID"] == "25777195"
    assert soundcloud_details["Account created"] == "2014-04-10 12:00 UTC"
    assert soundcloud_details["Profile record modified"] == "2017-04-14 14:12 UTC"
    assert soundcloud_details["Tracks"] == "2"
    assert soundcloud_details["Playlists"] == "1"
    assert soundcloud_details["Reposts"] == "3"
    assert soundcloud_details["Comments"] == "5"
    events = soundcloud.metadata["timeline_events"]
    assert events[0]["label"] == "Account registered"
    assert events[1]["label"] == "Profile record modified"
    assert "not asserted as user activity" in events[1]["note"]


def test_timeline_semantics_do_not_mislabel_feed_or_record_dates() -> None:
    adapter = MaigretAdapter()
    findings = adapter.normalise_results(
        [
            {
                "site": "Medium",
                "url": "https://medium.com/@example",
                "status": "claimed",
                "username": "example",
                "latest_activity_at": "2026-08-05 11:02:46 UTC",
                "_extractor": "Medium RSS",
            },
            {
                "site": "Chess",
                "url": "https://www.chess.com/member/example",
                "status": "claimed",
                "username": "example",
                "latest_activity_at": "2026-08-02 11:02:47 UTC",
                "_extractor": "Chess.com API",
            },
            {
                "site": "Hive Blog",
                "url": "https://hive.blog/@example",
                "status": "claimed",
                "username": "example",
                "latest_activity_at": "2019-03-13T15:55:15",
                "_extractor": "Hive Blog",
            },
            {
                "site": "MixCloud",
                "url": "https://www.mixcloud.com/example/",
                "status": "claimed",
                "username": "example",
                "created_at": "2011-02-10T17:47:24Z",
                "updated_at": "2011-02-10T17:47:24Z",
                "_extractor": "Mixcloud API",
            },
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )

    medium = findings[0].metadata["timeline_events"][0]
    assert medium["type"] == "feed_generated"
    assert medium["label"] == "RSS feed generated"
    assert medium["include_in_timeline"] is False
    assert medium["source_kind"] == "rss"
    assert "lastBuildDate" in medium["note"]

    chess = findings[1].metadata["timeline_events"][0]
    assert chess["type"] == "last_seen"
    assert chess["label"] == "Last online"
    assert chess["semantic"] == "platform_last_online"
    assert chess["confidence"] == "high"

    hive = findings[2].metadata["timeline_events"][0]
    assert hive["type"] == "public_activity"
    assert hive["label"] == "Latest public activity"
    assert hive["semantic"] == "latest_public_activity"

    mixcloud = findings[3].metadata["timeline_events"]
    assert mixcloud[0]["type"] == "registered"
    assert mixcloud[1]["type"] == "profile_updated"
    assert mixcloud[1]["include_in_timeline"] is False
    assert "identical" in mixcloud[1]["note"]


def test_additional_platform_metrics_and_identifiers_are_presented() -> None:
    adapter = MaigretAdapter()
    findings = adapter.normalise_results(
        [
            {
                "site": "MixCloud",
                "url": "https://www.mixcloud.com/example/",
                "status": "claimed",
                "username": "example",
                "fullname": "Example DJ",
                "description": "Public profile description",
                "city": "Dublin",
                "country": "Ireland",
                "cloudcast_count": 24,
                "favorite_count": 116,
                "listen_count": 296,
                "is_pro": False,
                "is_premium": False,
            },
            {
                "site": "Keybase",
                "url": "https://keybase.io/example",
                "status": "claimed",
                "username": "example",
                "github_username": "example-github",
                "twitter_username": "example_x",
            },
            {
                "site": "Paypal",
                "url": "https://paypal.me/example",
                "status": "claimed",
                "username": "example",
                "payerId": "PAYER123",
                "primaryCurrencyCode": "EUR",
                "isProfileStatusActive": True,
            },
            {
                "site": "Imgur",
                "url": "https://imgur.com/user/example",
                "status": "claimed",
                "username": "example",
                "imgur_profile_avatar_url": "https://imgur.com/user/example/avatar",
                "reputation_count": "12.5",
                "reputation_name": "Trusted",
                "all_views_count": 900,
            },
            {
                "site": "Paragraph",
                "url": "https://paragraph.xyz/@example",
                "status": "claimed",
                "username": "example",
                "paragraph_user_id": "paragraph-123",
                "wallet_address": "0x1234567890abcdef",
            },
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )

    mixcloud = details_for(findings[0])
    assert findings[0].profile.biography == "Public profile description"
    assert findings[0].profile.location == "Dublin, Ireland"
    assert mixcloud["Cloudcasts"] == "24"
    assert mixcloud["Likes / favourites"] == "116"
    assert mixcloud["Listens"] == "296"
    assert mixcloud["Pro account"] == "No"
    assert mixcloud["Premium account"] == "No"

    keybase = details_for(findings[1])
    assert keybase["GitHub username"] == "example-github"
    assert keybase["Twitter username"] == "example_x"

    paypal = details_for(findings[2])
    assert paypal["Payer ID"] == "PAYER123"
    assert paypal["Primary currency"] == "EUR"
    assert paypal["Profile active"] == "Yes"

    imgur = details_for(findings[3])
    assert findings[3].profile.avatar_url == "https://imgur.com/user/example/avatar"
    assert imgur["Reputation"] == "12.5"
    assert imgur["Reputation level"] == "Trusted"
    assert imgur["Views"] == "900"

    paragraph = details_for(findings[4])
    assert paragraph["Paragraph user ID"] == "paragraph-123"
    assert paragraph["Wallet address"] == "0x1234567890abcdef"


def test_common_username_platform_fields_are_promoted() -> None:
    findings = MaigretAdapter().normalise_results(
        [
            {
                "site": "Chess",
                "url": "https://www.chess.com/member/alex",
                "status": "claimed",
                "username": "alex",
                "chess_user_id": "15490860",
            },
            {
                "site": "Substack",
                "url": "https://substack.com/@alex",
                "status": "claimed",
                "username": "alex",
                "image": "https://example.test/original.png",
                "image_cdn": "https://example.test/cdn.webp",
            },
            {
                "site": "Periscope",
                "url": "https://www.pscp.tv/alex",
                "status": "claimed",
                "username": "alex",
                "twitter_uid": "7380362",
                "twitter_screen_name": "alex",
            },
            {
                "site": "memory.lol [Twitter]",
                "url": "https://api.memory.lol/v1/tw/alex",
                "status": "claimed",
                "username": "alex",
                "known_usernames": "['alex', 'old_alex']",
            },
        ],
        "alex",
        QueryType.USERNAME,
        "raw",
    )

    chess = details_for(findings[0])
    assert chess["Account ID"] == "15490860"

    assert findings[1].profile.avatar_url == "https://example.test/cdn.webp"

    periscope = details_for(findings[2])
    assert periscope["Twitter ID"] == "7380362"
    assert periscope["Twitter username"] == "alex"

    memory = details_for(findings[3])
    assert memory["Known usernames"] == "['alex', 'old_alex']"


def test_blackbird_platform_metadata_is_structured() -> None:
    finding = BlackbirdAdapter().normalise_results(
        [
            {
                "name": "TikTok",
                "url": "https://www.tiktok.com/oembed?url=https://www.tiktok.com/@example",
                "status": "FOUND",
                "metadata": [
                    {"name": "Name", "value": "Example Person"},
                    {"name": "Posts", "value": "12"},
                ],
            }
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )[0]
    details = details_for(finding)

    assert finding.status == FindingStatus.FOUND
    assert finding.canonical_url == "https://www.tiktok.com/@example"
    assert finding.metadata["evidence_url"].startswith("https://www.tiktok.com/oembed")
    assert finding.profile.display_name == "Example Person"
    assert details["Posts"] == "12"


def test_social_analyzer_text_is_parsed_for_supported_platforms() -> None:
    adapter = SocialAnalyzerAdapter()
    findings = adapter.normalise_results(
        [
            {
                "site": "johnsmith (John) · GitHub",
                "url": "https://github.com/johnsmith",
                "status": "unknown",
                "candidate_generator": "social_analyzer",
                "social_analyzer_title": "johnsmith (John) · GitHub",
                "social_analyzer_text": "John 18 followers · following",
            },
            {
                "site": "John Smith | OK.RU",
                "url": "https://ok.ru/johnsmith",
                "status": "unknown",
                "candidate_generator": "social_analyzer",
                "social_analyzer_title": "John Smith | OK.RU",
                "social_analyzer_text": "Последний визит: 20 мая 2015 Фотографии",
            },
        ],
        "johnsmith",
        QueryType.USERNAME,
        "raw",
    )

    assert findings[0].platform == "GitHub"
    assert findings[0].profile.display_name == "John"
    assert findings[0].profile.account_id == "johnsmith"
    assert findings[0].profile.followers == 18
    assert findings[1].platform == "OK.RU"
    assert findings[1].profile.display_name == "John Smith"
    assert findings[1].metadata["timeline_events"][0]["timestamp"] == (
        "2015-05-20T00:00:00+00:00"
    )

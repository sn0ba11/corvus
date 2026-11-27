from app.models.db import Finding
from app.services.profile_links import extract_profile_link_candidates


def finding_with_links() -> Finding:
    return Finding(
        id="finding-links",
        query_type="username",
        queried_value="example_user",
        finding_type="profile",
        platform="Instagram",
        value="example_user",
        status="found",
        extra={
            "profile": {
                "biography": (
                    "Elsewhere: https://www.tiktok.com/@Example.User and "
                    "https://linktr.ee/exampleuser"
                )
            },
            "external_url": "https://github.com/example-user",
            "platform_details": [
                {"label": "Website", "value": "https://beacons.ai/exampleuser"}
            ],
        },
        source_tool="instaloader",
        source_tools=["instaloader"],
    )


def test_extracts_bounded_cross_platform_links_with_provenance() -> None:
    candidates = extract_profile_link_candidates(finding_with_links())
    by_platform = {candidate.platform: candidate for candidate in candidates}
    assert by_platform["TikTok"].account_identifier == "example.user"
    assert by_platform["TikTok"].provenance == "profile.biography"
    assert by_platform["GitHub"].account_identifier == "example-user"
    assert by_platform["Linktree"].account_identifier == "exampleuser"
    assert by_platform["Beacons"].account_identifier == "exampleuser"


def test_rejects_local_and_credential_bearing_links() -> None:
    finding = finding_with_links()
    finding.extra = {
        "profile": {
            "biography": (
                "http://127.0.0.1/admin https://user:secret@example.org/ "
                "https://t.me/public_handle"
            )
        }
    }
    candidates = extract_profile_link_candidates(finding)
    assert [(item.platform, item.account_identifier) for item in candidates] == [
        ("Telegram", "public_handle")
    ]


def test_link_candidate_limit_is_enforced() -> None:
    finding = finding_with_links()
    assert len(extract_profile_link_candidates(finding, limit=2)) == 2


def test_profile_account_from_url_accepts_only_account_shaped_pages() -> None:
    from app.services.profile_links import profile_account_from_url

    accepted = {
        "https://www.instagram.com/example.user/": ("Instagram", "example.user"),
        "https://www.tiktok.com/@example_user": ("TikTok", "example_user"),
        "https://x.com/example_user": ("X", "example_user"),
        "https://www.reddit.com/user/example_user/": ("Reddit", "example_user"),
        "https://www.linkedin.com/in/example-user": ("LinkedIn", "example-user"),
        "https://www.snapchat.com/add/example_user": ("Snapchat", "example_user"),
        "https://www.snapchat.com/@example_user": ("Snapchat", "example_user"),
        "https://t.me/example_user": ("Telegram", "example_user"),
        "https://www.threads.net/@example_user": ("Threads", "example_user"),
        "https://www.pinterest.com/example_user/": ("Pinterest", "example_user"),
        "https://m.facebook.com/example.user": ("Facebook", "example.user"),
    }
    for url, expected in accepted.items():
        assert profile_account_from_url(url) == expected, url
    rejected = (
        "https://www.instagram.com/p/Cabc123/", "https://www.instagram.com/explore/tags/x/",
        "https://www.instagram.com/reel/abc/", "https://www.tiktok.com/@example_user/video/1",
        "https://www.tiktok.com/discover", "https://x.com/example_user/status/1", "https://x.com/search",
        "https://www.reddit.com/r/example/comments/1/x/", "https://www.linkedin.com/company/example",
        "https://www.facebook.com/profile.php?id=1", "https://www.facebook.com/groups/example",
        "https://www.pinterest.com/pin/123456/", "https://www.pinterest.com/ideas/example/1234",
        "https://t.me/s/example_user", "https://t.me/+AbCdEf", "https://github.com/example/repo",
        "https://example.org/example_user", "https://www.youtube.com/watch?v=abc", "not a url",
        "http://localhost/example",
    )
    for url in rejected:
        assert profile_account_from_url(url) is None, url

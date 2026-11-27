import json
from pathlib import Path

import pytest
from PIL import Image

from app.adapters.base import ExecutionResult
from app.adapters.builtin import (
    MAX_SUBFINDER_FINDINGS,
    BingNameSearchAdapter,
    BlackbirdAdapter,
    HarvesterAdapter,
    HoleheAdapter,
    InstaloaderAdapter,
    MaigretAdapter,
    NexfilAdapter,
    SherlockAdapter,
    SocialAnalyzerAdapter,
    SubfinderAdapter,
)
from app.models.schemas import FindingStatus, QueryType

FIXTURES = Path(__file__).parents[1] / "fixtures"


def result(stdout: str = "") -> ExecutionResult:
    return ExecutionResult(0, stdout, "")


@pytest.mark.parametrize(
    "adapter,query_type,flag",
    [
        (MaigretAdapter(), QueryType.USERNAME, "--no-autoupdate"),
        (SherlockAdapter(), QueryType.USERNAME, "--local"),
        (BlackbirdAdapter(), QueryType.USERNAME, "--no-update"),
        (InstaloaderAdapter(), QueryType.USERNAME, "--username"),
        (NexfilAdapter(), QueryType.USERNAME, "-u"),
        (SocialAnalyzerAdapter(), QueryType.USERNAME, "--username"),
        (HoleheAdapter(), QueryType.EMAIL, "--only-used"),
        (HarvesterAdapter(), QueryType.DOMAIN, "-d"),
        (SubfinderAdapter(), QueryType.DOMAIN, "-d"),
        (BingNameSearchAdapter(), QueryType.NAME, "--output"),
    ],
)
def test_command_is_argument_array_and_disables_updates(
    tmp_path: Path, adapter, query_type, flag: str
) -> None:
    injection = "target; touch /tmp/pwned"
    command = adapter.build_command(injection, query_type, tmp_path, 60)
    assert isinstance(command, list)
    assert command[0] == adapter.executable
    assert injection in command
    assert flag in command
    assert all("shell=" not in item for item in command)


def test_subfinder_parses_jsonl_and_rejects_out_of_domain_hosts(tmp_path: Path) -> None:
    (tmp_path / "subfinder.jsonl").write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "host": "api.example.org",
                        "input": "example.org",
                        "sources": ["crtsh", "hackertarget"],
                    }
                ),
                json.dumps(
                    {"host": "*.cdn.example.org", "input": "example.org", "source": "crtsh"}
                ),
                json.dumps({"host": "unrelated.example.net", "input": "example.org"}),
                "not-json",
            )
        ),
        encoding="utf-8",
    )
    adapter = SubfinderAdapter()
    command = adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, timeout=240)
    records = adapter.parse_results(result(), tmp_path)
    findings = adapter.normalise_results(records, "example.org", QueryType.DOMAIN, "raw")

    assert command[command.index("-timeout") + 1] == "60"
    assert command[command.index("-o") + 1].endswith("subfinder.jsonl")
    assert {finding.value for finding in findings} == {"api.example.org", "cdn.example.org"}
    assert {finding.finding_type for finding in findings} == {"subdomain"}
    by_value = {finding.value: finding for finding in findings}
    assert by_value["api.example.org"].status == FindingStatus.FOUND
    assert by_value["api.example.org"].confidence == 0.9
    assert by_value["cdn.example.org"].status == FindingStatus.LIKELY
    assert by_value["cdn.example.org"].confidence == 0.65
    assert findings[0].source_tool == "subfinder"


def test_subfinder_bounds_large_passive_result_sets_and_prioritises_corroboration() -> None:
    adapter = SubfinderAdapter()
    records = [
        {
            "host": f"single-{index:04d}.example.org",
            "value": f"single-{index:04d}.example.org",
            "sources": ["one-source"],
        }
        for index in range(MAX_SUBFINDER_FINDINGS + 50)
    ]
    records.extend(
        {
            "host": f"strong-{index}.example.org",
            "value": f"strong-{index}.example.org",
            "sources": ["source-a", "source-b"],
        }
        for index in range(5)
    )

    findings = adapter.normalise_results(records, "example.org", QueryType.DOMAIN, "raw")

    assert len(findings) == MAX_SUBFINDER_FINDINGS
    assert {f"strong-{index}.example.org" for index in range(5)}.issubset(
        {finding.value for finding in findings}
    )
    assert all(
        finding.status == FindingStatus.FOUND
        for finding in findings
        if str(finding.value).startswith("strong-")
    )
    assert all(finding.metadata["upstream_record_count"] == MAX_SUBFINDER_FINDINGS + 55 for finding in findings)


def test_blackbird_uses_bounded_per_site_timeout(tmp_path: Path) -> None:
    command = BlackbirdAdapter().build_command("example", QueryType.USERNAME, tmp_path, timeout=240)
    timeout_index = command.index("--timeout")
    assert command[timeout_index + 1] == "15"
    assert BlackbirdAdapter.default_timeout == 240


def test_nexfil_honours_selected_http_proxy(tmp_path: Path) -> None:
    command = NexfilAdapter().build_command(
        "example",
        QueryType.USERNAME,
        tmp_path,
        timeout=90,
        proxy="http://127.0.0.1:8080",
    )

    assert command == [
        "nexfil",
        "-u",
        "example",
        "-t",
        "15",
        "-proto",
        "http",
        "-ph",
        "127.0.0.1",
        "-pp",
        "8080",
    ]


def test_nexfil_drops_unsupported_socks_proxy(tmp_path: Path) -> None:
    command = NexfilAdapter().build_command(
        "example",
        QueryType.USERNAME,
        tmp_path,
        timeout=90,
        proxy="socks5h://127.0.0.1:9050",
    )

    assert command == ["nexfil", "-u", "example", "-t", "15"]


def test_instaloader_honours_selected_proxy(tmp_path: Path) -> None:
    command = InstaloaderAdapter().build_command(
        "example",
        QueryType.USERNAME,
        tmp_path,
        timeout=90,
        proxy="socks5h://127.0.0.1:9050",
    )

    assert command == [
        "instaloader-osint",
        "--username",
        "example",
        "--proxy",
        "socks5h://127.0.0.1:9050",
    ]


def test_instaloader_profile_and_local_avatar_are_normalised(tmp_path: Path) -> None:
    (tmp_path / "profile_pic.jpg").write_bytes(b"local-image-evidence")
    (tmp_path / "profile.json").write_text(
        """{
          "site": "Instagram",
          "status": "found",
          "url": "https://www.instagram.com/example/",
          "username": "example",
          "account_id": "12345",
          "display_name": "Example Person",
          "biography": "Public biography",
          "avatar_url": "https://cdn.example/profile.jpg",
          "avatar_file": "profile_pic.jpg",
          "avatar_quality": "hd",
          "verified": false,
          "followers": 123,
          "following": 45,
          "session_loaded": true
        }""",
        encoding="utf-8",
    )
    adapter = InstaloaderAdapter()
    records = adapter.parse_results(result(), tmp_path)
    finding = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")[0]

    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.95
    assert finding.finding_type == "instagram_profile"
    assert finding.profile.display_name == "Example Person"
    assert finding.profile.avatar_url == "https://cdn.example/profile.jpg"
    assert finding.metadata["avatar_quality"] == "hd"
    assert finding.metadata["avatar_local_path"] == str(tmp_path / "profile_pic.jpg")


def test_instaloader_fingerprints_collector_image_immediately(tmp_path: Path) -> None:
    output_dir = tmp_path / "data" / "raw" / "case" / "run"
    output_dir.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="navy").save(output_dir / "profile_pic.jpg")
    (output_dir / "profile.json").write_text(
        json.dumps(
            {
                "site": "Instagram",
                "status": "found",
                "avatar_url": "https://cdn.example/profile.jpg",
                "avatar_file": "profile_pic.jpg",
            }
        ),
        encoding="utf-8",
    )

    record = InstaloaderAdapter().parse_results(result(), output_dir)[0]

    assert record["avatar_local_path"] == str(output_dir / "profile_pic.jpg")
    assert record["image_cache"]["source"] == "collector_local_file"
    assert record["image_cache"]["mime_type"] == "image/jpeg"
    assert len(record["image_cache"]["sha256"]) == 64


def test_nexfil_urls_are_unverified_candidates(tmp_path: Path) -> None:
    (tmp_path / "example.txt").write_text(
        "[+] https://ok.ru/example\n[+] https://github.com/example\n",
        encoding="utf-8",
    )
    adapter = NexfilAdapter()
    records = adapter.parse_results(result("https://ok.ru/example"), tmp_path)
    findings = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")

    assert {finding.canonical_url for finding in findings} == {
        "https://ok.ru/example",
        "https://github.com/example",
    }
    assert {finding.status for finding in findings} == {FindingStatus.UNKNOWN}
    assert {finding.confidence for finding in findings} == {0.10}
    assert {finding.finding_type for finding in findings} == {"unverified_profile_lead"}


def test_social_analyzer_urls_are_unverified_candidates(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(
        """{
          "profiles": [
            {
              "website": "Example Social",
              "link": "https://social.example/example",
              "rate": 92,
              "title": "Example Person"
            }
          ]
        }""",
        encoding="utf-8",
    )
    adapter = SocialAnalyzerAdapter()
    records = adapter.parse_results(result(), tmp_path)
    finding = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")[0]

    assert finding.platform == "Example Social"
    assert finding.canonical_url == "https://social.example/example"
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.confidence == 0.15
    assert finding.metadata["social_analyzer_rate"] == 92


def test_maigret_parse_and_normalise(tmp_path: Path) -> None:
    (tmp_path / "report_example_simple.json").write_bytes((FIXTURES / "maigret.json").read_bytes())
    (tmp_path / "maigret-data.json").write_text('{"sites": {}}', encoding="utf-8")
    adapter = MaigretAdapter()
    records = adapter.parse_results(ExecutionResult(0, "", "", session_loaded=True), tmp_path)
    findings = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")
    # A raw "Claimed" is an uncorroborated site-checker claim, not a confirmed
    # account; it starts as unknown until verification or another collector
    # independently confirms it (see test_maigret_claimed_site_is_not_defaulted_to_found).
    assert [f.status for f in findings] == [FindingStatus.UNKNOWN, FindingStatus.NOT_FOUND]
    assert findings[0].confidence == 0.15
    assert all(f.metadata["session_loaded"] is True for f in findings)
    command = adapter.build_command("example", QueryType.USERNAME, tmp_path, 60)
    assert command[command.index("--db") + 1].endswith("maigret-data.json")
    assert command[command.index("--timeout") + 1] == "15"
    assert "--no-recursion" in command
    assert "--no-extracting" not in command


def test_maigret_claimed_site_is_not_defaulted_to_found(tmp_path: Path) -> None:
    (tmp_path / "report_example_simple.json").write_bytes((FIXTURES / "maigret.json").read_bytes())
    adapter = MaigretAdapter()
    records = adapter.parse_results(result(), tmp_path)
    findings = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")

    assert records[0]["status"] == "Claimed"
    assert records[1]["status"] == "Unclaimed"
    assert findings[0].status == FindingStatus.UNKNOWN
    assert findings[0].confidence == 0.15
    assert findings[0].metadata["maigret_unverified_claim"] is True
    assert findings[0].finding_type == "unverified_profile_lead"
    assert findings[1].status == FindingStatus.NOT_FOUND


def test_maigret_nested_extraction_is_flattened_without_site_configuration(tmp_path: Path) -> None:
    (tmp_path / "report_example_simple.json").write_text(
        """{
          "Instagram": {
            "site": {"headers": {"authorization": "must-not-persist"}},
            "username": "example",
            "url_user": "https://www.instagram.com/example/",
            "status": {
              "username": "example",
              "site_name": "Instagram",
              "url": "https://www.instagram.com/example/",
              "status": "Claimed",
              "ids": {
                "fullname": "Example Person",
                "image": "https://cdn.example/avatar.jpg",
                "bio": "Public biography",
                "location": "Dublin",
                "follower_count": "1,234",
                "following_count": "56"
              }
            },
            "http_status": 200,
            "rank": 4
          }
        }""",
        encoding="utf-8",
    )
    records = MaigretAdapter().parse_results(result(), tmp_path)
    finding = MaigretAdapter().normalise_results(records, "example", QueryType.USERNAME, "raw")[0]

    assert records[0]["site"] == "Instagram"
    assert "authorization" not in str(records[0])
    assert finding.canonical_url == "https://www.instagram.com/example"
    assert finding.profile.avatar_url == "https://cdn.example/avatar.jpg"
    assert finding.profile.display_name == "Example Person"
    assert finding.profile.biography == "Public biography"
    assert finding.profile.location == "Dublin"
    assert finding.profile.followers == 1234
    assert finding.profile.following == 56
    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.70
    assert finding.metadata["maigret_evidence_strength"] == "extracted_account_metadata"
    assert "fullname" in finding.metadata["maigret_evidence_fields"]


def test_maigret_public_duolingo_enrichment_is_normalised(tmp_path: Path) -> None:
    (tmp_path / "report_duolingo_simple.json").write_text(
        """{
          "Duolingo": {
            "username": "duolingo",
            "url_user": "https://www.duolingo.com/profile/duolingo",
            "status": {
              "username": "duolingo",
              "site_name": "Duolingo",
              "url": "https://www.duolingo.com/profile/duolingo",
              "status": "Claimed",
              "ids": {}
            }
          }
        }""",
        encoding="utf-8",
    )
    (tmp_path / "maigret-public-enrichment.json").write_text(
        """{
          "Duolingo": {
            "id": 1723277568,
            "username": "Duolingo",
            "name": "Duolingo",
            "bio": "Language learning",
            "picture": "//cdn.example/duo.png",
            "profileCountry": "IE",
            "fromLanguage": "ro",
            "learningLanguage": "it",
            "streak": 42,
            "totalXp": 1370,
            "hasPlus": true,
            "creationDate": 1741000755,
            "courses": [
              {"fromLanguage": "ro", "learningLanguage": "it", "title": "Italian", "xp": 1347},
              {"fromLanguage": "ro", "learningLanguage": "zh", "title": "Chinese", "xp": 23}
            ]
          }
        }""",
        encoding="utf-8",
    )

    adapter = MaigretAdapter()
    records = adapter.parse_results(result(), tmp_path)
    finding = adapter.normalise_results(records, "duolingo", QueryType.USERNAME, "raw")[0]
    details = {item["label"]: item["value"] for item in finding.metadata["platform_details"]}

    assert finding.profile.avatar_url == "https://cdn.example/duo.png"
    assert finding.profile.display_name == "Duolingo"
    assert finding.profile.biography == "Language learning"
    assert finding.profile.location is None  # a bare country code is not a location
    assert details["Profile country"] == "IE"
    assert finding.profile.account_id == "Duolingo"
    assert details["Streak"] == "42"
    assert details["Total XP"] == "1,370"
    assert details["Current course"] == "Romanian (ro) → Italian (it)"
    assert "Romanian (ro) → Italian (it) · 1,347 XP" in details["Courses"]
    assert details["Duolingo Plus"] == "Yes"
    assert finding.metadata["public_enrichment"] == "Duolingo"


def test_maigret_x_metadata_is_promoted_to_platform_details(tmp_path: Path) -> None:
    (tmp_path / "report_example_simple.json").write_text(
        """{
          "Twitter": {
            "username": "example",
            "url_user": "https://twitter.com/example",
            "status": {
              "site_name": "Twitter",
              "url": "https://twitter.com/example",
              "status": "Claimed",
              "ids": {
                "uid": "user-123",
                "fullname": "Example Person",
                "location": "Dublin, Ireland",
                "created_at": "2011-08-09 21:52:00+00:00",
                "follower_count": "34",
                "following_count": "188",
                "favourites_count": "241",
                "is_protected": "True"
              }
            }
          }
        }""",
        encoding="utf-8",
    )
    adapter = MaigretAdapter()
    finding = adapter.normalise_results(
        adapter.parse_results(result(), tmp_path), "example", QueryType.USERNAME, "raw"
    )[0]
    details = {item["label"]: item["value"] for item in finding.metadata["platform_details"]}

    assert finding.profile.location == "Dublin, Ireland"
    assert finding.profile.followers == 34
    assert finding.profile.following == 188
    assert details["Account created"] == "2011-08-09 21:52 UTC"
    assert details["Likes / favourites"] == "241"
    assert details["Protected account"] == "Yes"


def test_company_field_is_captured_in_platform_details(tmp_path: Path) -> None:
    (tmp_path / "report_example_simple.json").write_text(
        """{
          "GitHub": {
            "username": "example",
            "url_user": "https://github.com/example",
            "status": {
              "site_name": "GitHub",
              "url": "https://github.com/example",
              "status": "Claimed",
              "ids": {
                "fullname": "Example Person",
                "company": "Example Corp"
              }
            }
          }
        }""",
        encoding="utf-8",
    )
    adapter = MaigretAdapter()
    finding = adapter.normalise_results(
        adapter.parse_results(result(), tmp_path), "example", QueryType.USERNAME, "raw"
    )[0]
    details = {item["label"]: item["value"] for item in finding.metadata["platform_details"]}
    assert details["Company / employer"] == "Example Corp"


def test_rich_profile_fields_are_normalised() -> None:
    records = [
        {
            "site": "Example Social",
            "url": "https://social.example/example",
            "status": "found",
            "avatar": "https://cdn.example/avatar.jpg",
            "full_name": "Example Person",
            "bio": "Public biography",
            "city": "Dublin",
            "user_id": 12345,
            "is_verified": "yes",
            "followers_count": "1,234",
            "following_count": 56,
        }
    ]
    finding = SherlockAdapter().normalise_results(records, "example", QueryType.USERNAME, "raw")[0]
    assert finding.profile.avatar_url == "https://cdn.example/avatar.jpg"
    assert finding.profile.display_name == "Example Person"
    assert finding.profile.biography == "Public biography"
    assert finding.profile.location == "Dublin"
    assert finding.profile.account_id == "12345"
    assert finding.profile.verified is True
    assert finding.profile.followers == 1234
    assert finding.profile.following == 56


def test_explicit_username_fields_are_normalised_for_safe_pivoting() -> None:
    finding = BlackbirdAdapter().normalise_results(
        [
            {
                "site": "Example",
                "url": "https://example.test/profile",
                "status": "found",
                "username": "pivot_handle",
            }
        ],
        "person@example.org",
        QueryType.EMAIL,
        "raw",
    )[0]
    assert finding.profile.account_id == "pivot_handle"


def test_invalid_rich_profile_counts_are_ignored() -> None:
    finding = SherlockAdapter().normalise_results(
        [{"site": "Example", "url": "https://example.test/u", "followers": "unknown"}],
        "example",
        QueryType.USERNAME,
        "raw",
    )[0]
    assert finding.profile.followers is None


def test_sherlock_command_uses_boolean_csv_flag_and_output_folder(tmp_path: Path) -> None:
    command = SherlockAdapter().build_command("example", QueryType.USERNAME, tmp_path, 60)
    assert command.count("--csv") == 1
    assert command[command.index("--folderoutput") + 1] == str(tmp_path)
    assert command[command.index("--timeout") + 1] == "15"
    assert str(tmp_path / "sherlock.csv") not in command


def test_sherlock_parse(tmp_path: Path) -> None:
    (tmp_path / "example.csv").write_bytes((FIXTURES / "sherlock.csv").read_bytes())
    records = SherlockAdapter().parse_results(result(), tmp_path)
    assert records[0]["site"] == "GitHub"
    assert records[0]["url"].endswith("/example")
    assert records[0]["status"] == "Claimed"


def test_sherlock_real_exists_column_is_not_defaulted_to_found(tmp_path: Path) -> None:
    (tmp_path / "example.csv").write_text(
        "username,name,url_main,url_user,exists,http_status,response_time_s\n"
        "example,GitHub,https://github.com,https://github.com/example,Claimed,200,1.2\n"
        "example,Reddit,https://reddit.com,https://reddit.com/user/example,Unclaimed,404,0.8\n",
        encoding="utf-8",
    )
    adapter = SherlockAdapter()
    records = adapter.parse_results(result(), tmp_path)
    findings = adapter.normalise_results(records, "example", QueryType.USERNAME, "raw")

    assert records[0]["status"] == "Claimed"
    assert records[1]["status"] == "Unclaimed"
    assert findings[0].status == FindingStatus.UNKNOWN
    assert findings[0].confidence == 0.15
    assert findings[0].metadata["sherlock_unverified_claim"] is True
    assert findings[0].finding_type == "unverified_profile_lead"
    assert findings[1].status == FindingStatus.NOT_FOUND


def test_blackbird_json_parse(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_bytes((FIXTURES / "blackbird.json").read_bytes())
    assert len(BlackbirdAdapter().parse_results(result(), tmp_path)) == 2


def test_blackbird_bare_constructed_profile_url_is_unverified() -> None:
    finding = BlackbirdAdapter().normalise_results(
        [
            {
                "name": "ok.ru",
                "url": "https://ok.ru/example",
                "status": "FOUND",
                "category": "social",
            }
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )[0]
    assert finding.platform == "ok.ru"
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.confidence == 0.10
    assert finding.finding_type == "unverified_profile_lead"
    assert finding.profile.display_name is None
    assert finding.metadata["blackbird_unverified_claim"] is True
    assert finding.metadata["blackbird_evidence_strength"] == "bare_url_assertion"
    assert "name" not in finding.metadata
    assert "category" not in finding.metadata


def test_blackbird_public_first_party_verification_preserves_found_status(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text(
        json.dumps(
            [
                {
                    "name": "Roblox",
                    "url": "https://auth.roblox.com/v1/usernames/validate?username=corvusfixturek9",
                    "status": "FOUND",
                    "metadata": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "blackbird-public-enrichment.json").write_text(
        json.dumps(
            {
                "Roblox": {
                    "blackbird_public_verification": {
                        "source": "https://users.roblox.com/v1/usernames/users",
                        "result": "exact_username_match",
                    },
                    "roblox_user_id": 1761042963,
                    "roblox_username": "CorvusFixtureK9",
                    "roblox_display_name": "CorvusFixtureK9",
                    "roblox_verified": False,
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = BlackbirdAdapter()
    records = adapter.parse_results(result(), tmp_path)
    finding = adapter.normalise_results(records, "corvusfixturek9", QueryType.USERNAME, "raw")[0]

    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.85
    assert finding.profile.account_id == "CorvusFixtureK9"
    assert finding.profile.display_name == "CorvusFixtureK9"
    assert finding.metadata["platform_id"] == 1761042963
    assert finding.metadata["blackbird_evidence_strength"] == "first_party_api_verification"


def test_blackbird_requires_extracted_account_metadata_for_found_status() -> None:
    finding = BlackbirdAdapter().normalise_results(
        [
            {
                "name": "Example Social",
                "url": "https://social.example/example",
                "status": "FOUND",
                "metadata": [
                    {
                        "schema": "HTML",
                        "type": "Text",
                        "name": "Full Name",
                        "value": "Person Example",
                    },
                    {
                        "schema": "HTML",
                        "type": "Text",
                        "name": "Location",
                        "value": "Dublin",
                    },
                    {
                        "schema": "HTML",
                        "type": "Number",
                        "name": "Follower Count",
                        "value": "1,234",
                    },
                ],
            }
        ],
        "example",
        QueryType.USERNAME,
        "raw",
    )[0]

    assert finding.status == FindingStatus.FOUND
    assert finding.confidence == 0.80
    assert finding.profile.display_name == "Person Example"
    assert finding.profile.location == "Dublin"
    assert finding.profile.followers == 1234
    assert finding.metadata["blackbird_evidence_strength"] == "extracted_metadata"
    assert finding.metadata["blackbird_evidence_fields"] == [
        "Full Name",
        "Location",
        "Follower Count",
    ]


def test_holehe_parse_and_rate_limit() -> None:
    text = (FIXTURES / "holehe.txt").read_text()
    adapter = HoleheAdapter()
    records = adapter.parse_results(result(text), Path("."))
    assert [r["status"] for r in records] == ["found", "rate_limited", "error"]
    assert [r["site"] for r in records] == ["github.com", "twitter.com", "example-error.test"]
    assert adapter.detect_rate_limit(text)
    assert not adapter.detect_rate_limit("[+] Email used, [x] Rate limit, [!] Error")


def test_holehe_filters_generic_email_legend_records() -> None:
    adapter = HoleheAdapter()
    records = adapter.parse_results(
        result("[+] amazon.com\n[+] Email used, [-] Email not used, [x] Rate limit, [!] Error"),
        Path("."),
    )
    assert [record["site"] for record in records] == ["amazon.com"]
    assert (
        adapter.normalise_results(
            [{"site": "Email", "status": "found", "value": "Email"}],
            "x@example.test",
            QueryType.EMAIL,
            "raw",
        )
        == []
    )


def test_harvester_parse(tmp_path: Path) -> None:
    (tmp_path / "harvest.json").write_bytes((FIXTURES / "harvest.json").read_bytes())
    records = HarvesterAdapter().parse_results(result(), tmp_path)
    assert {r["type"] for r in records} == {"email", "host", "ip", "url", "name"}


def test_harvester_findings_stay_found_unlike_maigret_unverified_claims(tmp_path: Path) -> None:
    # theHarvester's hits are passive-DNS/certificate-transparency facts, not
    # probabilistic username-existence claims, so they must not be swept up by
    # MaigretAdapter's uncorroborated-claim downgrade even though HarvesterAdapter
    # inherits from MaigretAdapter.
    (tmp_path / "harvest.json").write_bytes((FIXTURES / "harvest.json").read_bytes())
    adapter = HarvesterAdapter()
    records = adapter.parse_results(result(), tmp_path)
    findings = adapter.normalise_results(records, "example.com", QueryType.DOMAIN, "raw")
    assert findings
    assert all(f.status == FindingStatus.FOUND for f in findings)
    assert all("maigret_unverified_claim" not in f.metadata for f in findings)


@pytest.mark.parametrize(
    "adapter,filename",
    [
        (MaigretAdapter(), "x.json"),
        (BlackbirdAdapter(), "x.json"),
        (HarvesterAdapter(), "harvest.json"),
    ],
)
def test_malformed_output_raises_clean_parse_error(tmp_path: Path, adapter, filename: str) -> None:
    (tmp_path / filename).write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.parse_results(result(), tmp_path)


def test_rate_limit_detection() -> None:
    assert MaigretAdapter.detect_rate_limit("HTTP 429 too many requests")
    assert MaigretAdapter.detect_rate_limit("status code: 429")
    assert not MaigretAdapter.detect_rate_limit("https://site.example/p/429/profile")
    assert not MaigretAdapter.detect_rate_limit("ordinary not found")

from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.deduplication import dedupe_key, deduplicate


def finding(tool: str, url: str) -> FindingRecord:
    return FindingRecord(
        query_type=QueryType.USERNAME,
        queried_value="example",
        finding_type="profile",
        platform="GitHub",
        value="example",
        canonical_url=url,
        status=FindingStatus.FOUND,
        source_tool=tool,
    )


def test_deduplication_preserves_all_sources() -> None:
    merged = deduplicate(
        [
            finding("maigret", "https://github.com/example"),
            finding("sherlock", "https://github.com/example"),
        ]
    )
    assert len(merged) == 1
    assert merged[0].sources == ["maigret", "sherlock"]


def test_profile_identity_merges_public_and_api_urls_for_same_platform_account() -> None:
    public_profile = FindingRecord(
        query_type=QueryType.USERNAME,
        queried_value="duolingo_test_user",
        finding_type="profile",
        platform="Duolingo",
        value="duolingo_test_user",
        canonical_url="https://www.duolingo.com/profile/duolingo_test_user",
        status=FindingStatus.FOUND,
        profile=ProfileEvidence(account_id="duolingo_test_user"),
        source_tool="maigret",
    )
    api_record = FindingRecord(
        query_type=QueryType.USERNAME,
        queried_value="duolingo_test_user",
        finding_type="profile",
        platform="Duolingo",
        value="duolingo_test_user",
        canonical_url=(
            "https://www.duolingo.com/2017-06-30/users?username=duolingo_test_user&_=1628308619574"
        ),
        status=FindingStatus.FOUND,
        source_tool="blackbird",
    )

    merged = deduplicate([public_profile, api_record])

    assert len(merged) == 1
    assert merged[0].sources == ["maigret", "blackbird"]


def test_fallback_dedupe_key_is_stable_for_equivalent_findings() -> None:
    first = FindingRecord(
        query_type=QueryType.NAME,
        queried_value="Example Person",
        finding_type="unstructured_note",
        status=FindingStatus.UNKNOWN,
        description="Same bounded collector output",
        source_tool="example_collector",
    )
    second = FindingRecord(
        query_type=QueryType.NAME,
        queried_value="Example Person",
        finding_type="unstructured_note",
        status=FindingStatus.UNKNOWN,
        description="Same bounded collector output",
        source_tool="example_collector",
    )

    assert dedupe_key(first) == dedupe_key(second)
    assert len(deduplicate([first, second])) == 1


def test_profile_identity_keeps_different_usernames_separate() -> None:
    left = finding("maigret", "https://github.com/example")
    right = finding("blackbird", "https://github.com/other")
    right.queried_value = "other"
    right.value = "other"

    assert len(deduplicate([left, right])) == 2

from app.services.account_correlation import (
    AccountEvidence,
    correlate_accounts,
    extract_domains,
    extract_urls,
    normalise_username,
)


def evidence(
    finding_id: str,
    *,
    username: str | None = None,
    display_name: str | None = None,
    biography: str | None = None,
    location: str | None = None,
    domains: frozenset[str] = frozenset(),
    urls: frozenset[str] = frozenset(),
    image_matches: dict[str, tuple[str, int]] | None = None,
) -> AccountEvidence:
    return AccountEvidence(
        finding_id,
        username,
        display_name,
        biography,
        location,
        domains,
        urls,
        image_matches or {},
    )


def test_normalises_username_variants() -> None:
    assert normalise_username("Some.User_01") == "someuser01"


def test_extracts_non_platform_domains_recursively() -> None:
    domains = extract_domains(
        {
            "bio": "See https://example.org/me and https://www.instagram.com/person",
            "links": ["https://sub.example.net/path"],
        }
    )
    assert domains == frozenset({"example.org", "sub.example.net"})


def test_extracts_normalized_reference_urls_without_fragments() -> None:
    urls = extract_urls(
        {
            "bio": "Read https://www.example.org/post/42/#section and https://example.org/post/42/",
            "links": ["https://example.net:443/a//b?x=1#ignored"],
        }
    )
    assert urls == frozenset({"https://example.org/post/42", "https://example.net/a/b?x=1"})


def test_shared_exact_url_is_a_stronger_signal_than_shared_domain() -> None:
    matches = correlate_accounts(
        {
            "a": evidence(
                "a",
                username="alpha",
                domains=frozenset({"example.org"}),
                urls=frozenset({"https://example.org/about"}),
            ),
            "b": evidence(
                "b",
                username="beta",
                domains=frozenset({"example.org"}),
                urls=frozenset({"https://example.org/about"}),
            ),
        },
        minimum_score=30,
    )
    assert len(matches) == 1
    assert matches[0].score == 30
    assert [signal["type"] for signal in matches[0].signals] == ["shared_url"]


def test_combines_transparent_signals_and_sorts_by_score() -> None:
    records = {
        "a": evidence(
            "a",
            username="Case.Person",
            display_name="Case Person",
            biography="Photographer based in Dublin working with rescue dogs",
            location="Dublin",
            domains=frozenset({"caseperson.ie"}),
            image_matches={"b": ("exact", 0)},
        ),
        "b": evidence(
            "b",
            username="case_person",
            display_name="Case Person",
            biography="Dublin photographer and rescue dogs volunteer",
            location="Dublin",
            domains=frozenset({"caseperson.ie"}),
        ),
        "c": evidence("c", username="unrelated"),
    }
    matches = correlate_accounts(records, minimum_score=35)

    assert len(matches) == 1
    assert matches[0].left_id == "a" and matches[0].right_id == "b"
    assert matches[0].score == 100
    assert {signal["type"] for signal in matches[0].signals} == {
        "username_exact",
        "display_name",
        "shared_domain",
        "location",
        "biography",
        "image",
    }


def test_similar_username_can_form_candidate() -> None:
    matches = correlate_accounts(
        {
            "a": evidence("a", username="distinctivehandle"),
            "b": evidence("b", username="distinctive_handle1"),
        },
        minimum_score=15,
    )
    assert matches and matches[0].signals[0]["type"] == "username_similar"

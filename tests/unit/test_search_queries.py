from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from app.models.db import SearchTemplate
from app.models.schemas import QueryType
from app.services.search_queries import (
    SearchQueryOptions,
    find_search_query,
    generate_google_dorks,
    generate_search_queries,
    sign_search_query,
    validate_template_definition,
    verify_search_query_token,
)


def test_google_dork_pack_prefers_current_operators_and_has_copyable_urls() -> None:
    dorks = generate_google_dorks("example_user", QueryType.USERNAME)

    assert dorks
    assert any(item.query == '"example_user"' for item in dorks)
    assert any("site:github.com" in item.query for item in dorks)
    assert any(item.reliability == "best_effort" and item.query.startswith("inurl:") for item in dorks)
    assert not any("cache:" in item.query or "related:" in item.query for item in dorks)
    assert all(parse_qs(urlsplit(item.url).query)["q"] == [item.query] for item in dorks)


def test_google_dork_pack_is_query_type_specific_and_keeps_derived_names() -> None:
    name_dorks = generate_google_dorks("Nova Example", QueryType.NAME)
    email_dorks = generate_google_dorks("investigator@example.org", QueryType.EMAIL)
    domain_dorks = generate_google_dorks("example.org", QueryType.DOMAIN)
    phone_dorks = generate_google_dorks("+353 87 123 4567", QueryType.PHONE)
    username_dorks = generate_google_dorks(
        "corvus_fixture_nq7az", QueryType.USERNAME, identity_names=("Nova Example",)
    )

    assert any("curriculum vitae" in item.query for item in name_dorks)
    assert any(item.query == '"investigator@example.org" -site:example.org' for item in email_dorks)
    assert any(item.query == 'site:example.org' for item in domain_dorks)
    assert any('"353871234567"' in item.query for item in phone_dorks)
    assert any(
        item.category == "Derived identity pivots"
        and item.query == '"Nova Example" "corvus_fixture_nq7az"'
        for item in username_dorks
    )


def test_email_query_pack_contains_exact_social_document_and_pivots() -> None:
    queries = generate_search_queries("investigator@example.org", QueryType.EMAIL)
    google = [item for item in queries if item.engine == "google"]
    assert any(item.query == '"investigator@example.org"' for item in google)
    assert any("site:github.com" in item.query for item in google)
    assert any("filetype:pdf" in item.query for item in google)
    assert any(item.query == '"investigator"' for item in google)
    assert any(item.query == '"example.org"' for item in google)


def test_name_query_pack_contains_identity_platform_document_and_handle_searches() -> None:
    queries = generate_search_queries("Nova Example", QueryType.NAME)
    google = [item for item in queries if item.engine == "google"]

    assert any(item.query == '"Nova Example"' for item in google)
    assert any(item.query == '"Nova Example" site:linkedin.com/in' for item in google)
    assert any(item.query == '"Nova Example" site:google.com/maps/contrib' for item in google)
    assert any("filetype:pdf" in item.query for item in google)
    assert any(
        item.query == '"Nova Example" (username OR handle OR "screen name" OR aka)'
        for item in google
    )
    assert any(
        item.query == '"Nova Example" (profile OR biography OR bio OR "about me")'
        for item in google
    )


def test_name_query_pack_contains_deterministic_variants_and_combined_search() -> None:
    queries = generate_search_queries("William Randolph Hearst", QueryType.NAME)
    google = [item for item in queries if item.engine == "google"]

    assert any(item.query == '"William R Hearst"' for item in google)
    assert any(item.query == '"Hearst, William"' for item in google)
    combined = next(item for item in google if item.id == "google-name-variants-combined")
    assert '"William Randolph Hearst" OR "William Hearst"' in combined.query
    assert any(
        item.id == "google-name-variants-platforms" and "site:linkedin.com/in" in item.query
        for item in google
    )


def test_name_variants_can_be_disabled_or_extended_with_known_aliases() -> None:
    disabled = SearchQueryOptions(
        engines=("google",),
        platform_ids=(),
        document_types=(),
        include_name_variants=False,
    )
    assert not any(
        item.category == "Name variants"
        for item in generate_search_queries("William Randolph Hearst", QueryType.NAME, disabled)
    )

    aliases = SearchQueryOptions(
        engines=("google",),
        platform_ids=(),
        document_types=(),
        name_aliases=("Bill Hearst",),
    )
    assert any(
        item.query == '"Bill Hearst"'
        for item in generate_search_queries("William Randolph Hearst", QueryType.NAME, aliases)
    )


def test_display_name_pivots_add_exact_linked_and_platform_queries() -> None:
    options = SearchQueryOptions(
        engines=("google",),
        platform_ids=("instagram", "linkedin"),
        document_types=(),
    )
    queries = generate_search_queries(
        "corvus_fixture_nq7az",
        QueryType.USERNAME,
        options,
        identity_names=("Nova Example",),
    )

    derived = [item for item in queries if item.category == "Derived identity pivots"]
    assert any(item.query == '"Nova Example"' for item in derived)
    assert any(item.query == '"Nova Example" "corvus_fixture_nq7az"' for item in derived)
    assert any(
        item.query == '"Nova Example" (site:instagram.com OR site:linkedin.com/in)'
        for item in derived
    )


def test_username_query_pack_contains_review_and_fitness_location_sources() -> None:
    queries = generate_search_queries("johnsmith", QueryType.USERNAME)
    google = [item for item in queries if item.engine == "google"]
    assert any(item.query == '"johnsmith" site:google.com/maps/contrib' for item in google)
    assert any(item.query == '"johnsmith" site:tripadvisor.com/Profile' for item in google)
    assert any(item.query == '"johnsmith" site:strava.com/athletes' for item in google)
    assert any(item.query == '"johnsmith" site:alltrails.com/members' for item in google)
    assert any(item.query == '"johnsmith" site:komoot.com/user' for item in google)
    assert any(
        item.query == '"johnsmith" site:connect.garmin.com/modern/profile' for item in google
    )
    assert any(
        item.query == '"johnsmith" (site:discord.com OR site:discord.gg)' for item in google
    )


def test_email_location_queries_use_local_part_not_full_address() -> None:
    queries = generate_search_queries("johnsmith@example.org", QueryType.EMAIL)
    item = next(query for query in queries if query.id == "google-location-strava")
    assert item.query == '"johnsmith" site:strava.com/athletes'


def test_selected_platform_documents_engine_and_date_range_are_respected() -> None:
    options = SearchQueryOptions(
        engines=("google",),
        platform_ids=("reddit", "strava"),
        document_types=("pdf", "pptx"),
        from_date=date(2023, 1, 2),
        to_date=date(2025, 6, 30),
    )
    queries = generate_search_queries("johnsmith", QueryType.USERNAME, options)

    assert queries and {item.engine for item in queries} == {"google"}
    assert any("site:reddit.com" in item.query for item in queries)
    assert any("site:strava.com/athletes" in item.query for item in queries)
    assert not any("site:github.com" in item.query for item in queries)
    documents = next(item for item in queries if item.id == "google-documents")
    assert "filetype:pdf" in documents.query and "filetype:pptx" in documents.query
    assert all("after:2023-01-02" in item.query for item in queries)
    assert all("before:2025-06-30" in item.query for item in queries)


def test_domain_query_pack_applies_selected_platforms() -> None:
    options = SearchQueryOptions(
        engines=("bing",),
        platform_ids=("linkedin", "reddit"),
        document_types=(),
    )
    queries = generate_search_queries("example.org", QueryType.DOMAIN, options)
    assert any(item.query == '"example.org" site:linkedin.com/in' for item in queries)
    assert any(item.query == '"example.org" site:reddit.com' for item in queries)


def test_reusable_template_renders_filters_and_automatic_date_range() -> None:
    template = SearchTemplate(
        id="template-1",
        name="Profile documents",
        category="Custom evidence",
        query_template="{quoted} {platforms} {filetypes}",
        engines=["google"],
        query_types=["username"],
    )
    options = SearchQueryOptions(
        engines=("google",),
        platform_ids=("github",),
        document_types=("pdf",),
        from_date=date(2024, 1, 1),
        include_core=False,
    )
    queries = generate_search_queries("analyst", QueryType.USERNAME, options, [template])

    custom = next(item for item in queries if item.id == "google-template-template-1")
    assert custom.query == '"analyst" site:github.com filetype:pdf after:2024-01-01'


def test_template_validation_rejects_unknown_or_identity_free_placeholders() -> None:
    with pytest.raises(ValueError, match="unsupported placeholder"):
        validate_template_definition(
            name="Bad",
            category="Custom",
            query_template="{quoted} {arbitrary}",
            engines=["google"],
            query_types=["username"],
        )
    with pytest.raises(ValueError, match="identity placeholder"):
        validate_template_definition(
            name="Bad",
            category="Custom",
            query_template="{platforms} {filetypes}",
            engines=["google"],
            query_types=["username"],
        )


def test_signed_query_token_is_bound_to_query_and_investigation() -> None:
    item = generate_search_queries("example", QueryType.USERNAME)[0]
    token = sign_search_query("secret", "case-one", item)

    verified = verify_search_query_token("secret", "case-one", token)
    assert verified.query == item.query
    assert verified.url == item.url
    with pytest.raises(ValueError):
        verify_search_query_token("secret", "case-two", token)
    encoded, signature = token.split(".", 1)
    replacement = "A" if signature[0] != "A" else "B"
    with pytest.raises(ValueError):
        verify_search_query_token(
            "secret",
            "case-one",
            f"{encoded}.{replacement}{signature[1:]}",
        )


def test_query_urls_encode_the_exact_generated_query() -> None:
    item = find_search_query("example", QueryType.USERNAME, "bing-social-reddit.com")
    assert item is not None
    assert parse_qs(urlsplit(item.url).query)["q"] == [item.query]


def test_unknown_query_id_is_not_launchable() -> None:
    assert find_search_query("example.com", QueryType.DOMAIN, "google-not-real") is None

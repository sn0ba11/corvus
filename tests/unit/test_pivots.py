from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.db import Base, Finding, Investigation, ToolRun
from app.services.handle_candidates import generate_handle_candidates
from app.services.pivots import (
    discover_email_pivots,
    discover_username_pivots,
    find_username_pivot,
    normalise_email,
    normalise_username,
    queue_automatic_email_pivots,
    queue_automatic_username_pivots,
    queue_email_pivot,
    queue_seeded_username_sweep,
    queue_username_pivot,
)


def _email_investigation(*, automatic: bool = False, query: str = "x@example.org") -> Investigation:
    investigation = Investigation(
        title="Email pivot test",
        query=query,
        query_type="email",
        selected_tools=["blackbird"],
        tags=[],
        auto_follow_username_pivots=automatic,
        status="completed",
    )
    source_run = ToolRun(
        tool_id="blackbird",
        display_name="Blackbird",
        status="completed",
        timeout_seconds=60,
    )
    investigation.runs.append(source_run)
    return investigation


def _username_investigation() -> Investigation:
    investigation = Investigation(
        title="Username pivot test",
        query="johnsmith",
        query_type="username",
        selected_tools=["blackbird"],
        tags=[],
        status="completed",
    )
    investigation.runs.append(
        ToolRun(
            tool_id="blackbird",
            display_name="Blackbird",
            status="completed",
            timeout_seconds=60,
            query_value="johnsmith",
            query_type="username",
        )
    )
    return investigation


def _finding(
    username: object,
    *,
    finding_id: str,
    platform: str = "Example",
    source_tool: str = "blackbird",
) -> Finding:
    return Finding(
        id=finding_id,
        query_type="email",
        queried_value="analyst@example.org",
        finding_type="profile",
        platform=platform,
        value=platform,
        status="found",
        extra={"profile": {"account_id": username}},
        source_tool=source_tool,
        source_tools=[source_tool],
        observed_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _username_finding(email: object, *, finding_id: str = "username-finding") -> Finding:
    return Finding(
        id=finding_id,
        query_type="username",
        queried_value="johnsmith",
        finding_type="profile",
        platform="Instagram",
        value="Instagram",
        status="found",
        extra={
            "public_email": email,
            "obfuscated_email": "me***@example.org",
        },
        source_tool="blackbird",
        source_tools=["blackbird"],
        observed_at=datetime(2026, 8, 17, tzinfo=UTC),
    )


def _attach(investigation: Investigation, finding: Finding) -> None:
    investigation.runs[0].findings.append(finding)
    investigation.findings.append(finding)


def test_email_normalisation_rejects_masked_or_invalid_values() -> None:
    assert normalise_email("Person@Example.ORG") == "Person@example.org"
    assert normalise_email("me***@example.org") is None
    assert normalise_email("masked@example") is None
    assert normalise_email("not-an-email") is None


def test_username_normalisation_rejects_unsafe_or_weak_values() -> None:
    assert normalise_username("@John.Smith") == "John.Smith"
    assert normalise_username("ab") is None
    assert normalise_username("analyst@example.org") is None
    assert normalise_username("https://example.org/user") is None
    assert normalise_username("123456") is None
    assert normalise_username("contains spaces") is None


def test_email_local_part_is_a_first_class_pivot_without_source_finding() -> None:
    investigation = _email_investigation(automatic=True, query="fixture_nq7az+case@example.org")

    candidates = discover_username_pivots(investigation)

    assert len(candidates) == 1
    assert candidates[0].username == "fixture_nq7az"
    assert candidates[0].finding_ids == ()
    assert candidates[0].platforms == ("Email address",)
    assert candidates[0].reasons == ("email.local_part",)


def test_discovers_and_deduplicates_explicit_email_username_fields() -> None:
    investigation = _email_investigation()
    findings = [
        _finding("JohnSmith", finding_id="finding-one", platform="GitHub"),
        _finding("johnsmith", finding_id="finding-two", platform="Reddit"),
        _finding("12345", finding_id="numeric", platform="Numeric ID"),
        Finding(
            id="metadata-login",
            query_type="email",
            queried_value="analyst@example.org",
            finding_type="profile",
            platform="Developer site",
            value="Developer site",
            status="found",
            extra={"raw": {"login": "dev_handle"}, "profile": {}},
            source_tool="blackbird",
            source_tools=["blackbird"],
            observed_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
    ]
    for finding in findings:
        _attach(investigation, finding)

    candidates = discover_username_pivots(investigation)

    assert [candidate.username.casefold() for candidate in candidates] == [
        "johnsmith",
        "dev_handle",
    ]
    john = candidates[0]
    assert john.finding_ids == ("finding-one", "finding-two")
    assert john.platforms == ("GitHub", "Reddit")
    assert john.followed is False


def test_existing_username_run_marks_candidate_followed() -> None:
    investigation = _email_investigation()
    _attach(investigation, _finding("johnsmith", finding_id="finding-one"))
    investigation.runs.append(
        ToolRun(
            tool_id="maigret",
            display_name="Maigret",
            status="completed",
            timeout_seconds=60,
            query_value="JohnSmith",
            query_type="username",
            pivot_depth=1,
        )
    )

    candidates = discover_username_pivots(investigation)

    assert len(candidates) == 1
    assert candidates[0].followed is True


def test_automatic_local_part_requires_independent_corroboration() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _email_investigation(automatic=True, query="fixture_nq7az@example.org")
        db.add(investigation)
        db.flush()

        assert queue_automatic_username_pivots(db, investigation) == []

        _attach(investigation, _finding("fixture_nq7az", finding_id="finding-corroboration"))
        db.flush()
        queued = queue_automatic_username_pivots(db, investigation)
        db.flush()

        assert len(queued) == 10
        assert {run.query_value for run in queued} == {"fixture_nq7az"}
        assert {run.pivot_source_finding_id for run in queued} == {"finding-corroboration"}
        assert all(run.pivot_automatic for run in queued)


def test_automatic_queueing_requires_two_independent_source_tools() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _email_investigation(automatic=True)
        _attach(investigation, _finding("auto_handle", finding_id="finding-auto"))
        _attach(
            investigation,
            _finding(
                "auto_handle",
                finding_id="finding-second-source",
                platform="Second platform",
                source_tool="holehe",
            ),
        )
        _attach(investigation, _finding("12345", finding_id="finding-numeric"))
        db.add(investigation)
        db.flush()

        queued = queue_automatic_username_pivots(db, investigation)
        db.flush()

        assert len(queued) == 10
        assert {run.query_value for run in queued} == {"auto_handle"}
        assert all(run.pivot_automatic for run in queued)
        assert {run.pivot_source_finding_id for run in queued} == {"finding-auto"}


def test_username_investigation_discovers_only_explicit_valid_email_pivots() -> None:
    investigation = _username_investigation()
    _attach(investigation, _username_finding("Person@Example.ORG"))

    candidates = discover_email_pivots(investigation)

    assert len(candidates) == 1
    assert candidates[0].email == "Person@example.org"
    assert candidates[0].finding_ids == ("username-finding",)
    assert candidates[0].reasons == ("metadata.public_email",)
    assert candidates[0].followed is False


def test_email_pivot_queues_only_email_capable_collectors_and_enforces_depth() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _username_investigation()
        _attach(investigation, _username_finding("person@example.org"))
        db.add(investigation)
        db.flush()

        candidate = discover_email_pivots(investigation)[0]
        queued = queue_email_pivot(db, investigation, candidate)
        db.flush()

        assert {run.tool_id for run in queued} == {"blackbird", "gravatar_public", "holehe", "pgp_public"}
        assert {run.query_value for run in queued} == {"person@example.org"}
        assert {run.query_type for run in queued} == {"email"}
        assert {run.pivot_depth for run in queued} == {1}
        assert {run.pivot_source_finding_id for run in queued} == {"username-finding"}

        investigation.runs[0].pivot_depth = 1
        second = _username_finding("second@example.org", finding_id="depth-one-email")
        _attach(investigation, second)
        db.flush()
        depth_candidate = next(
            item for item in discover_email_pivots(investigation) if item.email == "second@example.org"
        )
        depth_two = queue_email_pivot(db, investigation, depth_candidate)
        assert {run.pivot_depth for run in depth_two} == {2}

        investigation.runs[0].pivot_depth = 2
        third = _username_finding("third@example.org", finding_id="depth-two-email")
        _attach(investigation, third)
        db.flush()
        depth_three_candidate = next(
            item for item in discover_email_pivots(investigation) if item.email == "third@example.org"
        )
        assert queue_email_pivot(db, investigation, depth_three_candidate) == []


def test_automatic_email_pivot_requires_independent_corroboration() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _username_investigation()
        investigation.auto_follow_username_pivots = True
        first = _username_finding("person@example.org", finding_id="email-one")
        second = _username_finding("person@example.org", finding_id="email-two")
        second.source_tool = "maigret"
        second.source_tools = ["maigret"]
        _attach(investigation, first)
        _attach(investigation, second)
        db.add(investigation)
        db.flush()

        queued = queue_automatic_email_pivots(db, investigation)
        assert {run.tool_id for run in queued} == {"blackbird", "gravatar_public", "holehe", "pgp_public"}
        assert all(run.pivot_automatic for run in queued)
        assert {run.pivot_depth for run in queued} == {1}


def test_existing_email_run_marks_email_candidate_followed() -> None:
    investigation = _username_investigation()
    _attach(investigation, _username_finding("person@example.org"))
    investigation.runs.append(
        ToolRun(
            tool_id="holehe",
            display_name="Holehe",
            status="completed",
            timeout_seconds=60,
            query_value="Person@example.org",
            query_type="email",
            pivot_depth=1,
        )
    )
    assert discover_email_pivots(investigation)[0].followed is True


def test_role_address_local_parts_do_not_seed_username_pivots() -> None:
    investigation = _email_investigation(automatic=True, query="info@example.org")
    assert discover_username_pivots(investigation) == []


def test_pivot_depth_is_enforced_before_queueing() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _email_investigation()
        investigation.runs[0].pivot_depth = 2
        _attach(investigation, _finding("johnsmith", finding_id="finding-depth-one"))
        db.add(investigation)
        db.flush()

        candidate = discover_username_pivots(investigation)[0]
        assert queue_username_pivot(db, investigation, candidate, automatic=False) == []


def test_name_handle_seed_queues_bounded_manual_username_sweep() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = Investigation(
            title="Name handle seed",
            query="Alex Morgan",
            query_type="name",
            selected_tools=["bing_name"],
            tags=[],
            status="completed",
        )
        db.add(investigation)
        db.flush()
        candidate = generate_handle_candidates(investigation.query, limit=1)[0]
        queued = queue_seeded_username_sweep(db, investigation, candidate)
        db.flush()

        assert len(queued) == 10
        assert {run.query_value for run in queued} == {"alexmorgan"}
        assert {run.query_type for run in queued} == {"username"}
        assert {run.pivot_depth for run in queued} == {0}
        assert all(not run.pivot_automatic for run in queued)

        investigation.runs.extend(queued)
        assert queue_seeded_username_sweep(db, investigation, candidate) == []


def test_manual_and_automatic_queueing_are_bounded_and_audited() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _email_investigation(automatic=True)
        _attach(investigation, _finding("johnsmith", finding_id="finding-one"))
        db.add(investigation)
        db.flush()

        candidate = discover_username_pivots(investigation)[0]
        queued = queue_username_pivot(db, investigation, candidate, automatic=False)
        db.flush()

        assert {run.tool_id for run in queued} == {
            "maigret",
            "sherlock",
            "blackbird",
            "instaloader",
            "nexfil",
            "social_analyzer",
            "username_public_profiles",
            "snapchat_public",
            "telegram_public",
            "tiktok_public",
        }
        assert {run.query_value for run in queued} == {"johnsmith"}
        assert {run.query_type for run in queued} == {"username"}
        assert {run.pivot_depth for run in queued} == {1}
        assert {run.pivot_source_finding_id for run in queued} == {"finding-one"}
        assert not any(run.pivot_automatic for run in queued)

        # The newly queued run set makes the same username ineligible for another automatic pass.
        investigation.runs.extend(queued)
        assert queue_automatic_username_pivots(db, investigation) == []


def test_owner_listed_sites_and_search_hits_become_username_candidates() -> None:
    investigation = Investigation(id="case", title="t", query="primary_handle", query_type="username")
    investigation.runs = []
    profile = Finding(
        id="keybase", investigation_id="case", finding_type="profile", platform="Keybase",
        value="primary_handle", status="found", source_tool="username_public_profiles",
        source_tools=["username_public_profiles"],
        extra={
            "linked_accounts": [{"platform": "Reddit", "username": "reddit_name", "url": None}],
            "linked_sites": ["https://github.com/git_name", "https://github.com/git_name/repo",
                             "https://example.org/about", 7],
        },
    )
    search_hit = Finding(
        id="hit", investigation_id="case", finding_type="search_result", platform="instagram.com",
        value="x", status="likely", source_tool="search_browser_capture",
        source_tools=["search_browser_capture"],
        extra={"linked_account": {"platform": "Instagram", "username": "insta_name",
                                  "source": "search_result_url"}},
    )
    investigation.findings = [profile, search_hit]

    candidates = {item.username: item for item in discover_username_pivots(investigation)}

    assert set(candidates) == {"reddit_name", "git_name", "insta_name"}
    assert candidates["git_name"].reasons == ("linked_site.github",)
    assert candidates["insta_name"].source_tools == ("search_browser_capture",)


def test_search_hits_and_post_links_rank_last_and_never_follow_automatically() -> None:
    from app.services.pivots import _automatic_candidate_is_corroborated

    investigation = Investigation(id="case", title="t", query="Example Person", query_type="name")
    investigation.runs = []

    def hit(index: int, tools: list[str]) -> Finding:
        return Finding(
            id=f"hit-{index}", investigation_id="case", finding_type="search_result",
            platform="instagram.com", value="x", status="likely", source_tool=tools[0],
            source_tools=tools,
            extra={"linked_account": {"platform": "Instagram", "username": f"search_hit_{index:02d}"}},
        )

    investigation.findings = [
        *(hit(index, ["search_browser_capture", "searxng_name_search"]) for index in range(12)),
        Finding(
            id="channel", investigation_id="case", finding_type="telegram_public_profile",
            platform="Telegram", value="channel", status="likely", source_tool="telegram_public",
            source_tools=["telegram_public"], extra={"linked_sites": ["https://x.com/posted_handle"]},
        ),
        Finding(
            id="profile", investigation_id="case", finding_type="profile", platform="Keybase",
            value="owner", status="found", source_tool="username_public_profiles",
            source_tools=["username_public_profiles"],
            extra={"profile": {"account_id": "owner"},
                   "linked_accounts": [{"platform": "Reddit", "username": "stated_handle"}]},
        ),
    ]

    candidates = discover_username_pivots(investigation)

    # Profile-stated handles come first, so search hits cannot crowd them out of the list.
    assert {candidates[0].username, candidates[1].username} == {"owner", "stated_handle"}
    assert all(
        item.reasons in {("search_result.account_page",), ("post_link.x",)}
        for item in candidates[2:]
    )
    # The same URL seen by two search collectors is still one observation.
    assert not any(_automatic_candidate_is_corroborated(item) for item in candidates)
    post_link = next(
        item for item in discover_username_pivots(
            Investigation(id="c2", title="t", query="q", query_type="name", runs=[],
                          findings=[investigation.findings[12]])
        )
    )
    assert post_link.reasons == ("post_link.x",)
    assert not _automatic_candidate_is_corroborated(post_link)


def _many_corroborated(investigation: Investigation, count: int) -> None:
    for index in range(count):
        handle = f"handle{index:02d}"
        _attach(investigation, _finding(handle, finding_id=f"first-{index}"))
        _attach(
            investigation,
            _finding(handle, finding_id=f"second-{index}", platform="Other", source_tool="holehe"),
        )


def test_more_than_five_candidates_are_listed_but_automatic_follow_stays_bounded() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        investigation = _email_investigation(automatic=True)
        _many_corroborated(investigation, 8)
        db.add(investigation)
        db.flush()

        assert len(discover_username_pivots(investigation)) == 8
        queued = queue_automatic_username_pivots(db, investigation)

        assert len({run.query_value for run in queued}) == 5


def test_candidate_beyond_the_fifth_can_be_found_for_manual_follow() -> None:
    investigation = _email_investigation()
    _many_corroborated(investigation, 8)
    candidates = discover_username_pivots(investigation)
    seventh = candidates[6]

    found = find_username_pivot(investigation, seventh.username, seventh.finding_ids[0])

    assert found is not None
    assert found.username == seventh.username

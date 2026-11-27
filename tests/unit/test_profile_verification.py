from datetime import UTC, datetime

import httpx
import pytest

from app.models.db import Finding, Investigation
from app.services.profile_verification import (
    ProfileVerifier,
    VerificationResult,
    VerificationTarget,
    apply_verification_results,
    verification_targets,
)


async def public_resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def target(
    url: str,
    *,
    identifier: str = "example",
    tools: tuple[str, ...] = ("sherlock",),
    source_metadata: dict[str, dict] | None = None,
    profile: dict | None = None,
) -> VerificationTarget:
    return VerificationTarget(
        finding_id="finding-1",
        url=url,
        platform=None,
        identifier=identifier,
        source_tools=tools,
        source_metadata=source_metadata or {},
        profile=profile or {},
        finding_type="unverified_profile_lead",
        status="unknown",
    )


@pytest.mark.asyncio
async def test_github_api_confirms_exact_account_identifier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/users/example"
        return httpx.Response(
            200,
            json={
                "login": "example",
                "id": 123,
                "name": "Example Person",
                "avatar_url": "https://avatars.example/example.png",
                "location": "Dublin",
                "html_url": "https://github.com/example",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://github.com/example")
        )

    assert result.state == "confirmed_account"
    assert result.method == "github_api"
    assert result.matched_identifier is True
    assert result.evidence is not None
    assert result.evidence["name"] == "Example Person"


@pytest.mark.asyncio
async def test_soft_404_html_is_dead_profile_not_positive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><title>Profile not found</title><p>This account does not exist.</p></html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://ok.ru/example")
        )

    assert result.state == "dead_profile"
    assert result.method == "soft_404_html"
    assert result.status_code == 200


@pytest.mark.asyncio
async def test_javascript_shell_without_identifier_is_unresolved_not_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><title>Example Social</title><div id='app'></div><script src='/app.js'></script></html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://social.example/users/missinguser", identifier="missinguser")
        )

    assert result.state == "generic_response"
    assert result.matched_identifier is None
    assert "remains unresolved" in (result.limitation or "")


def _profile_page(handle: str) -> httpx.Response:
    return httpx.Response(
        200,
        text=f"<html><title>{handle}</title><p>Posts by {handle}</p></html>",
        headers={"content-type": "text/html"},
    )


@pytest.mark.asyncio
async def test_page_that_echoes_any_handle_is_not_a_likely_account() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return _profile_page(request.url.path.rsplit("/", 1)[-1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://social.example/users/example")
        )

    assert len(requested) == 2 and "example" not in requested[1].rsplit("/", 1)[-1]
    assert result.state == "generic_response"
    assert result.method == "control_handle_html"
    assert "made-up handle" in (result.limitation or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("control_status", [404, 302])
async def test_page_is_a_candidate_when_a_made_up_handle_is_refused(control_status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/example"):
            return _profile_page("example")
        return httpx.Response(control_status, headers={"location": "/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://social.example/users/example")
        )

    assert result.state == "unverified_candidate"
    assert result.matched_identifier is True


@pytest.mark.asyncio
async def test_page_is_not_a_candidate_when_the_control_request_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/example"):
            return _profile_page("example")
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://social.example/users/example")
        )

    assert result.state == "retrieval_error"
    assert "could not be compared" in (result.limitation or "")


@pytest.mark.parametrize(
    ("url", "handle", "kept"),
    [
        ("https://site.example/user/user", "user", "https://site.example/user/"),
        ("https://site.example/member.php?username=member", "member", "/member.php?username="),
        ("https://site.example/id/id?tab=id", "id", "https://site.example/id/id?tab="),
        ("https://site.example/@Example/posts", "example", "/posts"),
        ("https://site.example/~example", "example", "https://site.example/~zq"),
        ("https://site.example/files/example-cv.pdf", "example", "-cv.pdf"),
    ],
)
def test_control_address_replaces_only_the_handle(url: str, handle: str, kept: str) -> None:
    from app.services.profile_verification import _control_url

    built = _control_url(url, handle)
    assert built is not None
    control_url, control = built
    assert kept in control_url
    assert control_url.count(control) == 1
    assert len(control_url) - len(control) == len(url) - len(handle)


def test_control_address_is_not_built_when_the_handle_is_not_in_the_address() -> None:
    from app.services.profile_verification import _control_url

    assert _control_url("https://site.example/users/12345", "example") is None


@pytest.mark.asyncio
async def test_control_handle_replaces_an_account_hostname() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return _profile_page(request.url.host.split(".")[0])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://example.blogs.example/")
        )

    assert hosts[0] == "example.blogs.example" and hosts[1].endswith(".blogs.example")
    assert hosts[1] != hosts[0]
    assert result.state == "generic_response"


@pytest.mark.asyncio
async def test_generic_page_with_identifier_remains_unverified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><title>Search results</title><p>Results for example. Sign up.</p></html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target("https://social.example/users/example")
        )

    assert result.state == "generic_response"
    assert result.matched_identifier is True


@pytest.mark.asyncio
async def test_instaloader_profile_object_is_authoritative_without_second_request() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target(
                "https://www.instagram.com/example",
                tools=("instaloader",),
                source_metadata={
                    "instaloader": {
                        "status": "found",
                        "account_id": "12345",
                    }
                },
                profile={
                    "account_id": "12345",
                    "display_name": "Example Person",
                    "avatar_url": "https://cdn.example/avatar.jpg",
                },
            )
        )

    assert result.state == "confirmed_account"
    assert result.method == "instaloader_profile_object"


@pytest.mark.asyncio
async def test_inconclusive_instaloader_source_is_not_credited_as_confirmation() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target(
                "https://www.instagram.com/example",
                tools=("instaloader", "maigret", "blackbird"),
                source_metadata={
                    "instaloader": {"status": "unknown", "session_loaded": False},
                    "maigret": {"status": "Claimed", "id": "12345"},
                    "blackbird": {"status": "FOUND", "blackbird_evidence_fields": ["Avatar"]},
                },
                profile={
                    "account_id": "example",
                    "display_name": "Example Person",
                    "avatar_url": "https://cdn.example/avatar.jpg",
                },
            )
        )

    assert result.state == "corroborated_account"
    assert result.method == "independent_collectors_with_account_metadata"
    assert result.evidence is not None
    assert result.evidence["source_tools"] == ["maigret", "blackbird"]


@pytest.mark.asyncio
async def test_duolingo_collector_metadata_prevents_false_identifier_mismatch() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        result = await ProfileVerifier(resolver=public_resolver, client=client).verify(
            target(
                "https://www.duolingo.com/profile/duolingo_test_user",
                identifier="duolingo_test_user",
                tools=("maigret", "blackbird", "nexfil", "sherlock"),
                source_metadata={
                    "maigret": {"status": "Claimed"},
                    "blackbird": {"status": "FOUND"},
                    "nexfil": {"status": "unknown"},
                    "sherlock": {"status": "unknown"},
                },
                profile={
                    "account_id": "duolingo_test_user",
                    "display_name": "duolingo_test_user",
                    "avatar_url": "https://cdn.example/avatar.png",
                },
            )
        )

    assert result.state == "corroborated_account"
    assert result.method == "independent_collectors_with_account_metadata"
    assert result.matched_identifier is True
    assert result.evidence is not None
    assert result.evidence["source_tools"] == ["maigret", "blackbird"]


def test_apply_verification_downgrades_dead_weak_lead_and_upgrades_confirmed() -> None:
    investigation = Investigation(
        id="case-1",
        title="Verification",
        query="example",
        query_type="username",
        selected_tools=["sherlock"],
        tags=[],
        status="completed",
    )
    dead = Finding(
        id="dead",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="unverified_profile_lead",
        platform="Example",
        value="example",
        canonical_url="https://example.test/example",
        status="unknown",
        extra={},
        source_tool="sherlock",
        source_tools=["sherlock"],
    )
    confirmed = Finding(
        id="confirmed",
        tool_run_id="run-2",
        query_type="username",
        queried_value="example",
        finding_type="unverified_profile_lead",
        platform="GitHub",
        value="example",
        canonical_url="https://github.com/example",
        status="unknown",
        extra={},
        source_tool="sherlock",
        source_tools=["sherlock"],
    )
    investigation.findings.extend([dead, confirmed])
    now = datetime.now(UTC)

    counts = apply_verification_results(
        investigation,
        [
            VerificationResult(
                "dead",
                dead.canonical_url or "",
                "dead_profile",
                "soft_404_html",
                now,
                status_code=200,
            ),
            VerificationResult(
                "confirmed",
                confirmed.canonical_url or "",
                "confirmed_account",
                "github_api",
                now,
                status_code=200,
                matched_identifier=True,
            ),
        ],
    )

    assert counts == {"confirmed_account": 1, "dead_profile": 1}
    assert dead.status == "not_found"
    assert dead.confidence == 0.0
    assert dead.extra["verification"]["existence_state"] == "not_found"
    assert dead.extra["verification"]["existence_confidence"] == 0.99
    assert confirmed.status == "found"
    assert confirmed.confidence == 0.95
    assert confirmed.finding_type == "profile"


def test_authoritative_dead_check_overturns_stale_single_tool_positive() -> None:
    investigation = Investigation(
        id="case-dead-positive",
        title="Dead positive",
        query="example",
        query_type="username",
        selected_tools=["blackbird"],
        tags=[],
        status="completed",
    )
    finding = Finding(
        id="stale-positive",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Example",
        value="example",
        canonical_url="https://social.example/example",
        status="found",
        confidence=0.8,
        extra={},
        source_tool="blackbird",
        source_tools=["blackbird"],
    )
    investigation.findings.append(finding)

    counts = apply_verification_results(
        investigation,
        [
            VerificationResult(
                "stale-positive",
                finding.canonical_url or "",
                "dead_profile",
                "soft_404_html",
                datetime.now(UTC),
                status_code=200,
                matched_identifier=False,
            )
        ],
    )

    assert counts == {"dead_profile": 1}
    assert finding.status == "not_found"
    assert finding.confidence == 0.0
    assert finding.extra["verification"]["existence_state"] == "not_found"
    assert finding.finding_type == "dead_profile"


def test_likely_html_account_is_distinct_from_unresolved_low_confidence_lead() -> None:
    investigation = Investigation(
        id="case-likely",
        title="Likely account",
        query="example",
        query_type="username",
        selected_tools=["sherlock"],
        tags=[],
        status="completed",
    )
    finding = Finding(
        id="likely",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="unverified_profile_lead",
        platform="Example",
        value="example",
        canonical_url="https://social.example/example",
        status="unknown",
        confidence=0.15,
        extra={},
        source_tool="sherlock",
        source_tools=["sherlock"],
    )
    investigation.findings.append(finding)

    apply_verification_results(
        investigation,
        [
            VerificationResult(
                "likely",
                finding.canonical_url or "",
                "unverified_candidate",
                "bounded_html",
                datetime.now(UTC),
                status_code=200,
                matched_identifier=True,
            )
        ],
    )

    assert finding.status == "likely"
    assert finding.confidence == 0.65
    assert finding.finding_type == "profile"
    assert finding.extra["verification"]["existence_state"] == "likely"
    assert finding.extra["verification"]["existence_confidence"] == 0.70


def test_verification_targets_cover_more_than_the_old_twenty_five_lead_cap() -> None:
    investigation = Investigation(
        id="case-many-targets",
        title="Many targets",
        query="example",
        query_type="username",
        selected_tools=["sherlock"],
        tags=[],
        status="completed",
    )
    for index in range(30):
        investigation.findings.append(
            Finding(
                id=f"candidate-{index}",
                tool_run_id="run-1",
                query_type="username",
                queried_value="example",
                finding_type="unverified_profile_lead",
                platform=f"Example {index}",
                value="example",
                canonical_url=f"https://social{index}.example/example",
                status="unknown",
                confidence=0.15,
                extra={},
                source_tool="sherlock",
                source_tools=["sherlock"],
            )
        )

    targets = verification_targets(investigation)

    assert len(targets) == 30


def test_verification_targets_include_unknown_technical_leads() -> None:
    investigation = Investigation(
        id="case-2",
        title="Targets",
        query="example",
        query_type="username",
        selected_tools=["blackbird"],
        tags=[],
        status="completed",
    )
    finding = Finding(
        id="candidate",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="unverified_profile_lead",
        platform="Example",
        value="example",
        canonical_url="https://social.example/example",
        status="unknown",
        extra={},
        source_tool="blackbird",
        source_tools=["blackbird"],
    )
    investigation.findings.append(finding)

    targets = verification_targets(investigation)

    assert [item.finding_id for item in targets] == ["candidate"]


def test_revalidation_demotes_a_lead_that_an_echoing_page_had_marked_likely() -> None:
    investigation = Investigation(
        id="case-echo",
        title="Echo",
        query="example",
        query_type="username",
        selected_tools=["sherlock"],
        tags=[],
        status="completed",
    )
    promoted = Finding(
        id="promoted",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Echo",
        value="example",
        canonical_url="https://echo.test/example",
        status="likely",
        confidence=0.65,
        extra={"verification": {"state": "unverified_candidate", "method": "bounded_html"}},
        source_tool="sherlock",
        source_tools=["sherlock", "maigret"],
    )
    investigation.findings.append(promoted)

    apply_verification_results(
        investigation,
        [
            VerificationResult(
                "promoted",
                promoted.canonical_url or "",
                "generic_response",
                "control_handle_html",
                datetime.now(UTC),
                status_code=200,
            )
        ],
    )

    assert promoted.status == "unknown"
    assert promoted.finding_type == "unverified_profile_lead"
    assert promoted.confidence == 0.05


def _evidence_backed_finding() -> tuple[Investigation, Finding]:
    investigation = Investigation(
        id="case-evidence",
        title="Evidence",
        query="example",
        query_type="username",
        selected_tools=["maigret"],
        tags=[],
        status="completed",
    )
    finding = Finding(
        id="backed",
        tool_run_id="run-1",
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Myspace",
        value="example",
        canonical_url="https://myspace.test/example",
        status="found",
        confidence=0.70,
        extra={},
        source_tool="maigret",
        source_tools=["maigret"],
    )
    investigation.findings.append(finding)
    return investigation, finding


@pytest.mark.parametrize(
    ("state", "method"),
    [("generic_response", "bounded_html"), ("unverified_candidate", "bounded_html")],
)
def test_inconclusive_page_does_not_lower_a_finding_with_collector_evidence(
    state: str, method: str
) -> None:
    investigation, finding = _evidence_backed_finding()

    apply_verification_results(
        investigation,
        [VerificationResult("backed", finding.canonical_url or "", state, method, datetime.now(UTC))],
    )

    assert finding.status == "found"
    assert finding.finding_type == "profile"
    assert (finding.confidence or 0) >= 0.65


def test_identifier_mismatch_still_lowers_a_single_source_finding() -> None:
    investigation, finding = _evidence_backed_finding()

    apply_verification_results(
        investigation,
        [
            VerificationResult(
                "backed",
                finding.canonical_url or "",
                "identifier_mismatch",
                "github_api",
                datetime.now(UTC),
            )
        ],
    )

    assert finding.status == "unknown"
    assert finding.finding_type == "unverified_profile_lead"

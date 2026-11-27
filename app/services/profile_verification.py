import asyncio
import ipaddress
import json
import re
import secrets
import socket
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from app.models.db import Finding, Investigation
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

Resolver = Callable[[str, int], Awaitable[list[str]]]
MAX_VERIFY_TARGETS = 250
MAX_RESPONSE_BYTES = 1024 * 1024
PROFILE_STATES = {
    "confirmed_account",
    "corroborated_account",
    "unverified_candidate",
    "dead_profile",
    "access_blocked",
    "rate_limited",
    "identifier_mismatch",
    "generic_response",
    "retrieval_error",
}
SOFT_404_MARKERS = (
    "page not found",
    "profile not found",
    "user not found",
    "account not found",
    "this account doesn't exist",
    "this account does not exist",
    "user does not exist",
    "page isn't available",
    "page is not available",
    "content unavailable",
    "error 404",
)
GENERIC_MARKERS = (
    "search results",
    "sign up",
    "create an account",
    "log in to continue",
    "login to continue",
)
EXISTENCE_STATE_BY_PROFILE_STATE = {
    "confirmed_account": "confirmed",
    "corroborated_account": "confirmed",
    "unverified_candidate": "likely",
    "dead_profile": "not_found",
    "access_blocked": "blocked",
    "rate_limited": "rate_limited",
    "identifier_mismatch": "mismatch",
    "generic_response": "unresolved",
    "retrieval_error": "unresolved",
}
EXISTENCE_CONFIDENCE_BY_PROFILE_STATE = {
    "confirmed_account": 0.99,
    "corroborated_account": 0.99,
    "unverified_candidate": 0.70,
    "dead_profile": 0.99,
    "access_blocked": 0.0,
    "rate_limited": 0.0,
    "identifier_mismatch": 0.75,
    "generic_response": 0.0,
    "retrieval_error": 0.0,
}


class ProfileVerificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VerificationTarget:
    finding_id: str
    url: str
    platform: str | None
    identifier: str
    source_tools: tuple[str, ...]
    source_metadata: dict[str, dict[str, Any]]
    profile: dict[str, Any]
    finding_type: str
    status: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    finding_id: str
    source_url: str
    state: str
    method: str
    observed_at: datetime
    status_code: int | None = None
    matched_identifier: bool | None = None
    evidence: dict[str, Any] | None = None
    limitation: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "existence_state": EXISTENCE_STATE_BY_PROFILE_STATE[self.state],
            "existence_confidence": EXISTENCE_CONFIDENCE_BY_PROFILE_STATE[self.state],
            "method": self.method,
            "source_url": self.source_url,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "status_code": self.status_code,
            "matched_identifier": self.matched_identifier,
            "evidence": self.evidence or {},
            "limitation": self.limitation,
        }


async def resolve_host(host: str, port: int) -> list[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


def _public_url(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProfileVerificationError("verification requires a public HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ProfileVerificationError("credential-bearing URLs cannot be verified")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _public_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise ProfileVerificationError("profile host did not resolve")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ProfileVerificationError("private or non-routable profile host is blocked")


def _profile_dict(finding: Finding) -> dict[str, Any]:
    value = finding.extra.get("profile", {})
    return dict(value) if isinstance(value, dict) else {}


def _source_metadata_dict(finding: Finding) -> dict[str, dict[str, Any]]:
    value = finding.extra.get("source_metadata", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(tool): dict(metadata) for tool, metadata in value.items() if isinstance(metadata, dict)
    }


def _identifier(finding: Finding) -> str:
    profile = _profile_dict(finding)
    for key in ("account_id", "username", "login", "handle"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = finding.queried_value or finding.value or ""
    return str(value).strip()


def verification_targets(
    investigation: Investigation, *, limit: int = MAX_VERIFY_TARGETS
) -> list[VerificationTarget]:
    candidates: list[tuple[int, VerificationTarget]] = []
    seen: set[str] = set()
    for finding in investigation.findings:
        if not finding.canonical_url or finding.finding_type in {"search_result", "host", "email"}:
            continue
        if finding.status not in {"found", "likely", "unknown"}:
            continue
        url = finding.canonical_url
        if url in seen:
            continue
        seen.add(url)
        source_tools = tuple(dict.fromkeys(finding.source_tools or [finding.source_tool]))
        profile = _profile_dict(finding)
        priority = 0
        if finding.finding_type == "unverified_profile_lead":
            priority += 60
        if finding.status in {"found", "likely"}:
            priority += 30
        if profile.get("account_id"):
            priority += 10
        if profile.get("avatar_url"):
            priority += 5
        if len(source_tools) > 1:
            priority += 10
        candidates.append(
            (
                priority,
                VerificationTarget(
                    finding_id=finding.id,
                    url=url,
                    platform=finding.platform,
                    identifier=_identifier(finding),
                    source_tools=source_tools,
                    source_metadata=_source_metadata_dict(finding),
                    profile=profile,
                    finding_type=finding.finding_type,
                    status=finding.status,
                ),
            )
        )
    return [
        item for _, item in sorted(candidates, key=lambda pair: (-pair[0], pair[1].url))[:limit]
    ]


def _positive_source_tools(target: VerificationTarget) -> tuple[str, ...]:
    if not target.source_metadata:
        return target.source_tools
    positive = []
    for tool, metadata in target.source_metadata.items():
        source_status = str(metadata.get("status", "")).casefold().replace("_", " ")
        if source_status in {"found", "claimed", "exists", "true", "likely"}:
            positive.append(tool)
    return tuple(dict.fromkeys(positive))


def _normalised_account_identifier(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").lstrip("@")).casefold()


def _collector_metadata_result(target: VerificationTarget) -> VerificationResult | None:
    host = (urlsplit(target.url).hostname or "").casefold()
    positive_sources = _positive_source_tools(target)
    rich_fields = {
        key: target.profile.get(key)
        for key in ("account_id", "display_name", "avatar_url", "biography", "location")
        if target.profile.get(key) not in (None, "")
    }
    instaloader_metadata = target.source_metadata.get("instaloader", {})
    if (
        "instagram.com" in host
        and "instaloader" in positive_sources
        and instaloader_metadata.get("account_id")
    ):
        return VerificationResult(
            finding_id=target.finding_id,
            source_url=target.url,
            state="confirmed_account",
            method="instaloader_profile_object",
            observed_at=datetime.now(UTC),
            matched_identifier=True,
            evidence=rich_fields,
        )
    account_id = rich_fields.get("account_id")
    identifier_matches = bool(
        account_id
        and _normalised_account_identifier(account_id)
        == _normalised_account_identifier(target.identifier)
    )
    if len(positive_sources) > 1 and identifier_matches and rich_fields.get("avatar_url"):
        return VerificationResult(
            finding_id=target.finding_id,
            source_url=target.url,
            state="corroborated_account",
            method="independent_collectors_with_account_metadata",
            observed_at=datetime.now(UTC),
            matched_identifier=True,
            evidence={"source_tools": list(positive_sources), **rich_fields},
        )
    if (
        positive_sources
        and identifier_matches
        and any(
            rich_fields.get(key) for key in ("avatar_url", "display_name", "biography", "location")
        )
    ):
        return VerificationResult(
            finding_id=target.finding_id,
            source_url=target.url,
            state="confirmed_account",
            method="collector_account_metadata",
            observed_at=datetime.now(UTC),
            matched_identifier=True,
            evidence={"source_tools": list(positive_sources), **rich_fields},
        )
    return None


def _username_from_url(target: VerificationTarget) -> str:
    if target.identifier:
        return target.identifier.lstrip("@").casefold()
    parts = [part for part in urlsplit(target.url).path.split("/") if part]
    return parts[-1].casefold() if parts else ""


class ProfileVerifier:
    def __init__(
        self,
        *,
        resolver: Resolver = resolve_host,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
        concurrency: int = 6,
    ) -> None:
        self.resolver = resolver
        self.client = client
        self.max_bytes = max_bytes
        self.concurrency = max(1, min(concurrency, 8))

    async def verify_many(self, targets: list[VerificationTarget]) -> list[VerificationResult]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded(target: VerificationTarget) -> VerificationResult:
            async with semaphore:
                return await self.verify(target)

        return await asyncio.gather(*(bounded(target) for target in targets))

    async def verify(self, target: VerificationTarget) -> VerificationResult:
        metadata_result = _collector_metadata_result(target)
        if metadata_result is not None:
            return metadata_result
        try:
            host, port = _public_url(target.url)
            if self.client is not None:
                _public_addresses(await self.resolver(host, port))
        except (ProfileVerificationError, OSError, ValueError) as exc:
            return VerificationResult(
                target.finding_id,
                target.url,
                "retrieval_error",
                "url_safety_validation",
                datetime.now(UTC),
                limitation=str(exc),
            )

        owns_client = self.client is None
        client = self.client or safe_client(
            timeout=httpx.Timeout(12.0),
            user_agent="OSINT-Workbench/1 (+local authorised research)",
            resolver=self.resolver,
        )
        try:
            host_key = host.casefold().removeprefix("www.")
            try:
                if host_key == "github.com":
                    return await self._verify_github(target, client)
                if host_key == "reddit.com" or host_key.endswith(".reddit.com"):
                    return await self._verify_reddit(target, client)
                if "gitlab" in host_key:
                    return await self._verify_gitlab(target, client)
                return await self._verify_html(target, client)
            except (httpx.HTTPError, EgressPolicyError, ProfileVerificationError, ValueError) as exc:
                return VerificationResult(
                    target.finding_id,
                    target.url,
                    "retrieval_error",
                    "platform_verifier",
                    datetime.now(UTC),
                    limitation=f"verification failed: {type(exc).__name__}",
                )
        finally:
            if owns_client:
                await client.aclose()

    async def _bounded_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        accept: str,
    ) -> tuple[httpx.Response, bytes]:
        request = client.build_request(
            "GET",
            url,
            headers={
                "Accept": accept,
                "User-Agent": "OSINT-Workbench/1 (+local authorised research)",
            },
        )
        response = await client.send(request, stream=True)
        try:
            content = await read_bounded_response(response, max_bytes=self.max_bytes)
        except EgressPolicyError as exc:
            raise ProfileVerificationError("verification response exceeded maximum size") from exc
        finally:
            await response.aclose()
        return response, content

    async def _json_request(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[httpx.Response, Any | None]:
        response, content = await self._bounded_request(client, url, accept="application/json")
        try:
            payload = json.loads(content) if content else None
        except json.JSONDecodeError:
            payload = None
        return response, payload

    async def _verify_github(
        self, target: VerificationTarget, client: httpx.AsyncClient
    ) -> VerificationResult:
        username = _username_from_url(target)
        response, payload = await self._json_request(
            client, f"https://api.github.com/users/{quote(username, safe='')}"
        )
        return self._api_identity_result(target, response, payload, username, "login", "github_api")

    async def _verify_reddit(
        self, target: VerificationTarget, client: httpx.AsyncClient
    ) -> VerificationResult:
        username = _username_from_url(target)
        response, payload = await self._json_request(
            client, f"https://www.reddit.com/user/{quote(username, safe='')}/about.json"
        )
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        return self._api_identity_result(target, response, payload, username, "name", "reddit_api")

    async def _verify_gitlab(
        self, target: VerificationTarget, client: httpx.AsyncClient
    ) -> VerificationResult:
        parsed = urlsplit(target.url)
        username = _username_from_url(target)
        response, payload = await self._json_request(
            client,
            f"{parsed.scheme}://{parsed.netloc}/api/v4/users?username={quote(username, safe='')}",
        )
        if response.status_code == 200 and isinstance(payload, list):
            matching = next(
                (
                    item
                    for item in payload
                    if isinstance(item, dict)
                    and str(item.get("username", "")).casefold() == username
                ),
                None,
            )
            if matching is not None:
                return VerificationResult(
                    target.finding_id,
                    target.url,
                    "confirmed_account",
                    "gitlab_api",
                    datetime.now(UTC),
                    status_code=200,
                    matched_identifier=True,
                    evidence={
                        key: matching.get(key)
                        for key in ("id", "username", "name", "avatar_url", "web_url", "state")
                        if matching.get(key) not in (None, "")
                    },
                )
            return VerificationResult(
                target.finding_id,
                target.url,
                "dead_profile",
                "gitlab_api",
                datetime.now(UTC),
                status_code=200,
                matched_identifier=False,
                limitation="GitLab returned no exact username",
            )
        return self._http_failure_result(target, response, "gitlab_api")

    def _api_identity_result(
        self,
        target: VerificationTarget,
        response: httpx.Response,
        payload: Any,
        username: str,
        identifier_key: str,
        method: str,
    ) -> VerificationResult:
        if response.status_code == 200 and isinstance(payload, dict):
            returned = str(payload.get(identifier_key, "")).casefold()
            matched = returned == username
            return VerificationResult(
                target.finding_id,
                target.url,
                "confirmed_account" if matched else "identifier_mismatch",
                method,
                datetime.now(UTC),
                status_code=200,
                matched_identifier=matched,
                evidence={
                    key: payload.get(key)
                    for key in (
                        identifier_key,
                        "id",
                        "name",
                        "avatar_url",
                        "bio",
                        "location",
                        "html_url",
                        "created_at",
                    )
                    if payload.get(key) not in (None, "")
                },
                limitation=None if matched else "platform identifier did not match the query",
            )
        return self._http_failure_result(target, response, method)

    def _http_failure_result(
        self, target: VerificationTarget, response: httpx.Response, method: str
    ) -> VerificationResult:
        if response.status_code in {404, 410}:
            state = "dead_profile"
        elif response.status_code in {401, 403}:
            state = "access_blocked"
        elif response.status_code == 429:
            state = "rate_limited"
        elif 300 <= response.status_code < 400:
            state = "generic_response"
        else:
            state = "retrieval_error"
        return VerificationResult(
            target.finding_id,
            target.url,
            state,
            method,
            datetime.now(UTC),
            status_code=response.status_code,
            limitation=f"platform returned HTTP {response.status_code}",
        )

    async def _verify_html(
        self, target: VerificationTarget, client: httpx.AsyncClient
    ) -> VerificationResult:
        try:
            response, content = await self._bounded_request(
                client,
                target.url,
                accept="text/html,application/xhtml+xml",
            )
        except (httpx.HTTPError, ProfileVerificationError) as exc:
            return VerificationResult(
                target.finding_id,
                target.url,
                "retrieval_error",
                "bounded_html",
                datetime.now(UTC),
                limitation=f"profile retrieval failed: {type(exc).__name__}",
            )
        if response.status_code != 200:
            return self._http_failure_result(target, response, "bounded_html")
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if mime_type not in {"text/html", "application/xhtml+xml"}:
            return VerificationResult(
                target.finding_id,
                target.url,
                "generic_response",
                "bounded_html",
                datetime.now(UTC),
                status_code=200,
                limitation=f"response was {mime_type or 'not HTML'}",
            )
        text_body = content.decode(response.encoding or "utf-8", errors="replace")
        raw_compact = re.sub(r"\s+", " ", unescape(text_body).casefold())
        text = unescape(re.sub(r"<[^>]+>", " ", text_body)).casefold()
        compact = re.sub(r"\s+", " ", text)
        if any(marker in compact for marker in SOFT_404_MARKERS):
            return VerificationResult(
                target.finding_id,
                target.url,
                "dead_profile",
                "soft_404_html",
                datetime.now(UTC),
                status_code=200,
                matched_identifier=False,
                limitation="page content reported a missing account",
            )
        identifier = _username_from_url(target)
        identifier_tokens = {
            identifier,
            f"@{identifier}" if identifier else "",
            quote(identifier, safe="").casefold() if identifier else "",
        }
        matched = bool(
            identifier
            and any(
                token and (token in compact or token in raw_compact) for token in identifier_tokens
            )
        )
        if not matched:
            return VerificationResult(
                target.finding_id,
                target.url,
                "generic_response",
                "bounded_html",
                datetime.now(UTC),
                status_code=200,
                matched_identifier=None,
                limitation=(
                    "HTTP 200 response did not expose the searched identifier in static HTML; "
                    "account existence remains unresolved"
                ),
            )
        method = "bounded_html"
        if any(marker in compact for marker in GENERIC_MARKERS):
            state = "generic_response"
            limitation = "page resembled a generic search, login or registration response"
        else:
            control = await self._control_handle_outcome(target, client, identifier)
            if control == "distinct":
                state = "unverified_candidate"
                limitation = (
                    "identifier was present and a made-up handle got a different response, "
                    "but no authoritative platform verifier is available"
                )
            elif control == "same":
                # Many sites answer HTTP 200 and echo whatever handle is in the address. In a
                # live control run on 2026-11-13 three sites did so for a handle that does
                # not exist, and each was marked "likely" on the strength of that echo.
                state = "generic_response"
                method = "control_handle_html"
                limitation = (
                    "the site returned the same kind of page for a made-up handle, so this "
                    "page does not show that the account exists"
                )
            else:
                # The comparison failed for a reason unrelated to the account (a block, a
                # rate limit). That supports nothing and must not erase a collector's evidence.
                state = "retrieval_error"
                method = "control_handle_html"
                limitation = (
                    "the page could not be compared with a made-up handle, so it does "
                    "not show that the account exists"
                )
        return VerificationResult(
            target.finding_id,
            target.url,
            state,
            method,
            datetime.now(UTC),
            status_code=200,
            matched_identifier=True,
            limitation=limitation,
        )


    async def _control_handle_outcome(
        self, target: VerificationTarget, client: httpx.AsyncClient, identifier: str
    ) -> str:
        """Request the same address for a made-up handle and compare the two answers.

        Returns "distinct" when the site treats the made-up handle differently (a missing
        page, a redirect, or a page that does not show it), "same" when it serves an
        equivalent page, and "inconclusive" when the comparison could not be made.
        """
        control = _control_url(target.url, identifier)
        if control is None:
            return "inconclusive"
        control_url, control_handle = control
        try:
            response, content = await self._bounded_request(
                client, control_url, accept="text/html,application/xhtml+xml"
            )
        except (httpx.HTTPError, EgressPolicyError, ProfileVerificationError):
            return "inconclusive"
        if response.status_code in {404, 410} or 300 <= response.status_code < 400:
            return "distinct"
        if response.status_code != 200:
            return "inconclusive"
        mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if mime_type not in {"text/html", "application/xhtml+xml"}:
            return "distinct"
        body = content.decode(response.encoding or "utf-8", errors="replace")
        raw_compact = re.sub(r"\s+", " ", unescape(body).casefold())
        compact = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", body)).casefold())
        if any(marker in compact for marker in (*SOFT_404_MARKERS, *GENERIC_MARKERS)):
            return "distinct"
        return "same" if control_handle in raw_compact or control_handle in compact else "distinct"


def _control_url(url: str, identifier: str) -> tuple[str, str] | None:
    """The same address with the handle swapped for a random one that nobody holds.

    Only the handle's own position is replaced. A handle that is also a word in the site's
    address pattern (/user/user, member.php?username=member) must not change the route, or
    the made-up request would reach a different page and look like a refusal.
    """
    if not identifier:
        return None
    alphabet = string.ascii_lowercase + string.digits
    control = "zq" + "".join(secrets.choice(alphabet) for _ in range(max(len(identifier), 12)))
    wanted = identifier.casefold()
    parsed = urlsplit(url)

    # 1. A query value that is exactly the handle (the last such value).
    pairs = parsed.query.split("&") if parsed.query else []
    for index in range(len(pairs) - 1, -1, -1):
        name, separator, value = pairs[index].partition("=")
        if separator and unquote(value).lstrip("@").casefold() == wanted:
            pairs[index] = f"{name}={control}"
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(pairs), "")), control

    # 2. A path segment that is the handle, allowing an @ or ~ prefix (the last such segment);
    #    failing that, the last segment that contains it as a whole token.
    segments = parsed.path.split("/")
    bounded = re.compile(rf"(?<![a-z0-9]){re.escape(identifier)}(?![a-z0-9])", re.IGNORECASE)
    exact = [i for i, part in enumerate(segments) if unquote(part).lstrip("@~").casefold() == wanted]
    partial = [i for i, part in enumerate(segments) if bounded.search(part)]
    if exact or partial:
        index = exact[-1] if exact else partial[-1]
        segments[index] = bounded.sub(control, segments[index], count=1)
        return urlunsplit((parsed.scheme, parsed.netloc, "/".join(segments), parsed.query, "")), control

    # 3. Some sites give each account its own hostname (<handle>.example.com).
    labels = parsed.netloc.split(".")
    if len(labels) >= 3 and labels[0].casefold() == wanted:
        netloc = ".".join([control, *labels[1:]])
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, "")), control
    return None


def apply_verification_results(
    investigation: Investigation, results: list[VerificationResult]
) -> dict[str, int]:
    findings = {finding.id: finding for finding in investigation.findings}
    counts: dict[str, int] = {state: 0 for state in PROFILE_STATES}
    for result in results:
        finding = findings.get(result.finding_id)
        if finding is None:
            continue
        metadata = dict(finding.extra)
        previous = metadata.get("verification")
        promoted_by_echo = (
            isinstance(previous, dict)
            and previous.get("state") == "unverified_candidate"
            and finding.status == "likely"
            and result.method == "control_handle_html"
        )
        metadata["verification"] = result.metadata()
        finding.extra = metadata
        counts[result.state] += 1
        # A lead that an earlier validation marked likely only because the page showed the
        # handle is still a weak lead, whatever status that promotion gave it.
        weak = (
            finding.finding_type == "unverified_profile_lead"
            or finding.status == "unknown"
            or promoted_by_echo
        )
        if result.state == "confirmed_account":
            finding.status = "found"
            finding.confidence = max(finding.confidence or 0.0, 0.95)
            finding.finding_type = "profile"
        elif result.state == "corroborated_account":
            finding.status = "found"
            finding.confidence = max(finding.confidence or 0.0, 0.98)
            finding.finding_type = "profile"
        elif result.state == "unverified_candidate":
            # This can raise a lead to likely; it is weaker than a collector's own account
            # evidence, so it never lowers a finding that is already found.
            if finding.status != "found" or weak:
                finding.status = "likely"
            finding.confidence = max(finding.confidence or 0.0, 0.65)
            finding.finding_type = "profile"
        elif result.state == "dead_profile":
            # An authoritative 404/410, exact API miss or explicit soft-404
            # overturns a current positive assertion. The prior collector state
            # remains visible in provenance and verification metadata.
            finding.status = "not_found"
            finding.confidence = 0.0
            finding.finding_type = "dead_profile"
        elif (result.state == "generic_response" and weak) or (
            result.state == "identifier_mismatch"
            and (weak or len(set(finding.source_tools or [finding.source_tool])) == 1)
        ):
            # A generic response (a redirect, a page with sign-up wording) says nothing about
            # the account either way, so it only keeps a weak lead weak. In a live run on
            # 2026-11-13 it had been erasing accounts for which a collector had extracted a
            # real account ID. A platform identifier that contradicts the query still counts
            # against a single-source finding.
            finding.status = "unknown"
            finding.confidence = min(finding.confidence or 0.05, 0.05)
            finding.finding_type = "unverified_profile_lead"
        elif weak and result.state in {"access_blocked", "rate_limited", "retrieval_error"}:
            finding.status = "unknown"
    return {key: value for key, value in counts.items() if value}

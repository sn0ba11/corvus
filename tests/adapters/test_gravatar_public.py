import json
from pathlib import Path

import httpx
import pytest

from app.adapters.gravatar_public import GravatarPublicAdapter, gravatar_identifier
from app.models.schemas import FindingStatus, QueryType


def test_gravatar_identifier_is_sha256_of_normalised_email() -> None:
    assert gravatar_identifier(" User@Example.org ") == gravatar_identifier("user@example.org")
    assert len(gravatar_identifier("user@example.org")) == 64


@pytest.mark.asyncio
async def test_gravatar_public_profile_is_bounded_and_normalised(tmp_path: Path) -> None:
    payload = {
        "profile_url": "https://gravatar.com/example",
        "avatar_url": "https://0.gravatar.com/avatar/hash",
        "display_name": "Example Person",
        "description": "Public biography",
        "location": "Dublin",
        "verified_accounts": [{"service_type": "github", "service_label": "example"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.gravatar.com"
        assert request.url.path.endswith(gravatar_identifier("user@example.org"))
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    adapter = GravatarPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("user@example.org", QueryType.EMAIL, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "user@example.org")
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(
        records, "user@example.org", QueryType.EMAIL, "gravatar-public-profile.json"
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == FindingStatus.FOUND
    assert finding.platform == "Gravatar"
    assert finding.profile.display_name == "Example Person"
    assert finding.profile.avatar_url == "https://0.gravatar.com/avatar/hash"
    assert finding.profile.location == "Dublin"
    assert finding.metadata["verified_accounts"][0]["service_type"] == "github"


@pytest.mark.asyncio
async def test_gravatar_404_is_not_a_positive_finding(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"Content-Type": "application/json"})

    adapter = GravatarPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("none@example.org", QueryType.EMAIL, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "none@example.org")
    assert result.returncode == 0
    assert adapter.parse_results(result, tmp_path) == []


@pytest.mark.asyncio
async def test_gravatar_rate_limit_is_structured(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Content-Type": "application/json"})

    adapter = GravatarPublicAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("user@example.org", QueryType.EMAIL, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "user@example.org")
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"

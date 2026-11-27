import json
from pathlib import Path

import httpx
import pytest

from app.adapters.github_discovery import GitHubDiscoveryAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_github_username_discovery_gets_exact_public_profile(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/users/octocat"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "login": "octocat",
                    "id": 1,
                    "html_url": "https://github.com/octocat",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1",
                    "name": "The Octocat",
                    "bio": "Public profile",
                    "location": "San Francisco",
                    "followers": 20,
                    "following": 0,
                    "created_at": "2008-01-14T04:33:35Z",
                }
            ).encode(),
        )

    adapter = GitHubDiscoveryAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("octocat", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "octocat")
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "octocat",
        QueryType.USERNAME,
        "github-public-discovery.json",
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.FOUND
    assert findings[0].confidence == 0.98
    assert findings[0].profile.followers == 20


@pytest.mark.asyncio
async def test_github_name_search_is_bounded_and_candidate_labelled(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/users":
            assert request.url.params["per_page"] == "5"
            assert "in:fullname" in request.url.params["q"]
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps({"items": [{"login": "example"}]}).encode(),
            )
        assert request.url.path == "/users/example"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "login": "example",
                    "id": 42,
                    "html_url": "https://github.com/example",
                    "name": "Example Person",
                }
            ).encode(),
        )

    adapter = GitHubDiscoveryAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("Example Person", QueryType.NAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "Example Person")
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "Example Person",
        QueryType.NAME,
        "github-public-discovery.json",
    )
    assert len(findings) == 1
    assert findings[0].status == FindingStatus.LIKELY
    assert findings[0].confidence == 0.65


@pytest.mark.asyncio
async def test_github_structured_rate_limit(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    adapter = GitHubDiscoveryAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("octocat", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "octocat")
    assert result.rate_limited is True
    assert result.error_code == "rate_limited"

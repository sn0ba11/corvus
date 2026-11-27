import json
from pathlib import Path

import httpx
import pytest

from app.adapters.github_developer_identity import GitHubDeveloperIdentityAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_github_developer_identity_collects_bounded_commits_and_keys(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/users/octocat":
            payload = {
                "login": "octocat",
                "id": 1,
                "html_url": "https://github.com/octocat",
                "name": "The Octocat",
            }
        elif path == "/users/octocat/repos":
            assert request.url.params["per_page"] == "3"
            payload = [
                {
                    "full_name": "octocat/Hello-World",
                    "html_url": "https://github.com/octocat/Hello-World",
                }
            ]
        elif path == "/users/octocat/gpg_keys":
            payload = [
                {
                    "id": 3,
                    "key_id": "3262EFF25BA0D270",
                    "public_key": "public-key-material",
                    "can_sign": True,
                    "revoked": False,
                    "created_at": "2016-03-24T11:31:04-06:00",
                    "emails": [
                        {
                            "email": "octocat@users.noreply.github.com",
                            "verified": True,
                        }
                    ],
                }
            ]
        elif path == "/users/octocat/ssh_signing_keys":
            payload = [
                {
                    "id": 2,
                    "key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2U=",
                    "title": "work key",
                    "created_at": "2020-06-11T21:31:57Z",
                }
            ]
        elif path == "/repos/octocat/Hello-World/commits":
            assert request.url.params["author"] == "octocat"
            assert request.url.params["per_page"] == "5"
            payload = [
                {
                    "sha": "abc123",
                    "author": {"login": "octocat", "id": 1},
                    "committer": {"login": "octocat", "id": 1},
                    "commit": {
                        "author": {
                            "name": "The Octocat",
                            "email": "octocat@users.noreply.github.com",
                            "date": "2026-09-01T12:00:00+01:00",
                        },
                        "committer": {
                            "name": "The Octocat",
                            "email": "octocat@example.org",
                            "date": "2026-09-01T12:01:00+01:00",
                        },
                        "verification": {
                            "verified": True,
                            "reason": "valid",
                            "verified_at": "2026-09-01T12:01:05Z",
                        },
                    },
                }
            ]
        else:
            raise AssertionError(f"unexpected request {request.url}")
        return httpx.Response(200, json=payload)

    adapter = GitHubDeveloperIdentityAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("octocat", QueryType.USERNAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "octocat")
    assert result.returncode == 0

    retained = json.loads((tmp_path / "github-developer-identity.json").read_text())
    assert retained["request_count"] == 5
    assert retained["commit_batches"][0]["repository"] == "octocat/Hello-World"

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "octocat",
        QueryType.USERNAME,
        "github-developer-identity.json",
    )
    kinds = [finding.finding_type for finding in findings]
    assert kinds.count("github_commit_identity") == 2
    assert "github_gpg_signing_key" in kinds
    assert "github_ssh_signing_key" in kinds
    author = next(
        finding
        for finding in findings
        if finding.finding_type == "github_commit_identity"
        and finding.metadata["role"] == "author"
    )
    assert author.status == FindingStatus.FOUND
    assert author.metadata["noreply_email"] is True
    assert author.metadata["signature_verified"] is True


@pytest.mark.asyncio
async def test_github_developer_identity_retains_rate_limit_outcome(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    adapter = GitHubDeveloperIdentityAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("octocat", QueryType.USERNAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "octocat")
    assert result.rate_limited is True
    retained = json.loads((tmp_path / "github-developer-identity.json").read_text())
    assert retained["status"] == "rate_limited"
    assert retained["request_count"] == 1

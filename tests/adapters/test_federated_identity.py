from pathlib import Path

import httpx
import pytest

from app.adapters.federated_identity import FederatedIdentityAdapter
from app.models.schemas import QueryType


@pytest.mark.asyncio
async def test_webfinger_activitypub_profile_and_aliases(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/webfinger":
            assert request.url.params["resource"] == "acct:alice@example.org"
            return httpx.Response(
                200,
                json={
                    "subject": "acct:alice@example.org",
                    "aliases": ["https://example.org/@alice"],
                    "links": [
                        {
                            "rel": "self",
                            "type": "application/activity+json",
                            "href": "https://example.org/users/alice",
                        }
                    ],
                },
            )
        if request.url.path == "/users/alice":
            return httpx.Response(
                200,
                json={
                    "id": "https://example.org/users/alice",
                    "type": "Person",
                    "preferredUsername": "alice",
                    "name": "Alice Example",
                    "url": "https://example.org/@alice",
                    "icon": {"url": "https://example.org/media/alice.jpg"},
                    "alsoKnownAs": ["https://social.example.net/@alice"],
                    "movedTo": "https://new.example.org/@alice",
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    adapter = FederatedIdentityAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("@alice@example.org", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "@alice@example.org")
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "@alice@example.org",
        QueryType.USERNAME,
        "federated-identity.json",
    )
    assert findings[0].finding_type == "federated_public_profile"
    assert findings[0].profile.display_name == "Alice Example"
    assert any(item.finding_type == "federated_alias" for item in findings)
    assert any(item.finding_type == "federated_migration_target" for item in findings)


@pytest.mark.asyncio
async def test_matrix_public_profile_uses_discovered_homeserver(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.org" and request.url.path == "/.well-known/matrix/client":
            return httpx.Response(
                200,
                json={"m.homeserver": {"base_url": "https://matrix.example.org"}},
            )
        if request.url.host == "matrix.example.org":
            assert request.url.path == "/_matrix/client/v3/profile/@alice:example.org"
            return httpx.Response(
                200,
                json={
                    "displayname": "Alice Matrix",
                    "avatar_url": "mxc://example.org/avatar",
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    adapter = FederatedIdentityAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("@alice:example.org", QueryType.USERNAME, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "@alice:example.org")
    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "@alice:example.org",
        QueryType.USERNAME,
        "federated-identity.json",
    )
    assert len(findings) == 1
    assert findings[0].platform == "Matrix"
    assert findings[0].profile.display_name == "Alice Matrix"
    assert findings[0].metadata["homeserver_base_url"] == "https://matrix.example.org"


def test_federated_identity_rejects_unscoped_username(tmp_path: Path) -> None:
    adapter = FederatedIdentityAdapter()
    assert QueryType.USERNAME not in adapter.supported_query_types
    assert QueryType.EMAIL in adapter.supported_query_types
    with pytest.raises(ValueError):
        adapter.build_command("alice", QueryType.USERNAME, tmp_path, 30)

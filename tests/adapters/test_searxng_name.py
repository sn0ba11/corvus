import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.adapters.searxng_name import (
    SEARXNG_USAGE_SCOPE,
    SearXNGNameSearchAdapter,
    build_searxng_username_queries,
    username_result_is_relevant,
    username_result_matches_query_spec,
)
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_searxng_name_search_collects_keyless_results_with_provenance(
    tmp_path: Path,
) -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/config":
            return httpx.Response(200, json={"instance_name": "Corvus Search"})
        assert request.url.path == "/search"
        params = parse_qs(request.url.query.decode())
        query = params["q"][0]
        assert params["engines"][0] == "google,mojeek,qwant"
        seen_queries.append(query)
        query_number = len(seen_queries)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://profiles.example.org/example-person",
                        "title": "Example Person public profile",
                        "content": "Example Person biography and public account.",
                        "engines": ["duckduckgo", "mojeek"],
                        "positions": [1, 3],
                        "score": 2.5,
                        "category": "general",
                    },
                    {
                        "url": f"https://records.example.net/result-{query_number}",
                        "title": f"Example Person record {query_number}",
                        "content": "A public record concerning Example Person.",
                        "engines": ["qwant"],
                        "positions": [2],
                    },
                    {
                        "url": f"https://shop.example.net/noise-{query_number}",
                        "title": "Unrelated product listing",
                        "content": "No subject name appears in this record.",
                        "engines": ["yahoo"],
                    },
                ],
                "unresponsive_engines": [["yep", "timeout"]],
            },
        )

    adapter = SearXNGNameSearchAdapter(httpx.MockTransport(handler), base_url="http://searxng.test")
    health = await adapter.healthcheck()
    command = adapter.build_command("Example Person", QueryType.NAME, tmp_path, 90)
    logs: list[tuple[str, str]] = []

    async def log(channel: str, line: str) -> None:
        logs.append((channel, line))

    result = await adapter.execute(command, tmp_path, 90, "Example Person", log)
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "Example Person", QueryType.NAME, "raw")

    assert health.healthy is True
    assert health.version == "Corvus Search"
    assert result.returncode == 0
    assert len(seen_queries) == 5
    assert all(not query.startswith("!") for query in seen_queries)
    assert len(records) == 6
    assert len(findings) == 6
    assert all(item.status == FindingStatus.LIKELY for item in findings)
    assert all(item.finding_type == "search_result" for item in findings)
    assert len(logs) == 5
    assert all(channel == "stdout" for channel, _ in logs)
    common = next(
        item
        for item in findings
        if item.canonical_url == "https://profiles.example.org/example-person"
    )
    assert common.metadata["usage_scope"] == SEARXNG_USAGE_SCOPE
    assert common.metadata["search_engines"] == ["duckduckgo", "mojeek"]
    assert len(common.metadata["query_matches"]) == 5
    payload = json.loads((tmp_path / "searxng-name-search.json").read_text(encoding="utf-8"))
    assert payload["usage_scope"] == SEARXNG_USAGE_SCOPE
    assert payload["rejected_irrelevant_count"] == 5
    assert len(payload["unresponsive_engines"]) == 5
    assert all((tmp_path / f"searxng-{item['id']}.json").is_file() for item in payload["queries"])


@pytest.mark.asyncio
async def test_searxng_name_search_surfaces_captcha_as_manual_action(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/config":
            return httpx.Response(200, json={"instance_name": "Corvus Search"})
        return httpx.Response(
            200,
            json={"results": [], "unresponsive_engines": [["qwant", "CAPTCHA"]]},
        )

    adapter = SearXNGNameSearchAdapter(httpx.MockTransport(handler), base_url="http://searxng.test")
    command = adapter.build_command("example_person", QueryType.USERNAME, tmp_path, 90)
    result = await adapter.execute(command, tmp_path, 90, "example_person")

    assert result.returncode == 0
    assert result.error_code == "manual_action_required"
    assert result.error_message is not None
    assert "qwant" in result.error_message
    assert "sock browser" in result.error_message


@pytest.mark.asyncio
async def test_searxng_name_search_reports_unavailable_local_service(tmp_path: Path) -> None:
    adapter = SearXNGNameSearchAdapter(
        httpx.MockTransport(lambda _: httpx.Response(503, text="unavailable")),
        base_url="http://searxng.test",
    )

    health = await adapter.healthcheck()

    assert health.healthy is False
    assert "health check failed" in health.message

    with pytest.raises(ValueError, match="name or username"):
        adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, 60)


def test_searxng_username_queries_are_bounded_and_username_specific() -> None:
    queries = build_searxng_username_queries("@example_person")

    assert len(queries) == 8
    assert queries[0]["query"] == '"example_person"'
    assert all("example_person" in item["query"] for item in queries)
    assert "site:instagram.com" in queries[1]["query"]
    assert queries[2]["query"] == '"example_person" site:instagram.com'
    assert {item["id"] for item in queries} == {
        "exact",
        "social",
        "instagram",
        "documents",
        "community", "gaming", "creator", "developer",
    }


def test_targeted_instagram_query_rejects_unrelated_bing_slop() -> None:
    record = {
        "url": "https://www.action.com/nl-nl/",
        "title": "Action Nederland: Kleine prijzen, grote glimlach | Action NL",
        "snippet": "Alles wat je nodig hebt, voor de allerlaagste prijs.",
    }
    assert not username_result_is_relevant("corvus_fixture_nq7az", record)
    assert not username_result_matches_query_spec("instagram", record)


def test_searxng_username_relevance_handles_url_encoding_and_separator_rendering() -> None:
    assert username_result_is_relevant(
        "example_person",
        {
            "url": "https://example.org/users/example_person",
            "title": "Public account",
            "snippet": None,
        },
    )
    assert username_result_is_relevant(
        "example_person",
        {
            "url": "https://example.org/profile",
            "title": "Example Person (@example-person)",
            "snippet": None,
        },
    )
    assert not username_result_is_relevant(
        "example_person",
        {
            "url": "https://example.org/unrelated",
            "title": "Completely unrelated account",
            "snippet": "No matching handle here.",
        },
    )


def test_username_relevance_rejects_substrings_and_search_pages():
    assert not username_result_is_relevant("alice", {"url": "https://example.org/alice_extra", "title": "alice_extra profile"})
    assert not username_result_is_relevant("alice", {"url": "https://example.org/search?q=alice", "title": "Search results"})
    assert not username_result_is_relevant("alice", {"url": "https://alice-extra.example.org/", "title": "Unrelated"})
    assert username_result_is_relevant("alice", {"url": "https://www.twitch.tv/alice", "title": "Live channel"})
    assert username_result_is_relevant("alice", {"url": "https://news.ycombinator.com/user?id=alice", "title": "User"})


@pytest.mark.asyncio
async def test_one_query_timeout_does_not_discard_later_targeted_hits(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    async def no_delay(*args):  # type: ignore[no-untyped-def]
        return None

    import app.adapters.searxng_name as module
    monkeypatch.setattr(module.asyncio, "sleep", no_delay)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        seen.append(query)
        if len(seen) == 1:
            raise httpx.ReadTimeout("controlled first-query timeout")
        results = [{"url": "https://www.instagram.com/alice", "title": "alice", "content": "Public profile"}] if query.endswith("site:instagram.com") else []
        return httpx.Response(200, json={"results": results})

    adapter = SearXNGNameSearchAdapter(httpx.MockTransport(handler), base_url="http://searxng.test")
    result = await adapter.execute(adapter.build_command("alice", QueryType.USERNAME, tmp_path, 90), tmp_path, 90, "alice")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "alice", QueryType.USERNAME, "raw")
    assert result.returncode == 0
    assert len(seen) == 8
    assert any(finding.canonical_url == "https://www.instagram.com/alice" for finding in findings)
    payload = json.loads((tmp_path / "searxng-name-search.json").read_text())
    assert payload["errors"][0]["query_id"] == "exact"


@pytest.mark.asyncio
async def test_searxng_username_search_collects_and_normalises_web_results(tmp_path: Path) -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        params = parse_qs(request.url.query.decode())
        request_query = params["q"][0]
        assert params["engines"][0] == "google,mojeek,qwant"
        seen_queries.append(request_query)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://forum.example.org/u/example_person",
                        "title": "example_person profile",
                        "content": "Posts by example_person",
                        "engines": ["brave"],
                        "positions": [1],
                    },
                    {
                        "url": "https://example.net/noise",
                        "title": "Different account",
                        "content": "No relevant username here",
                        "engines": ["bing"],
                    },
                ],
                "unresponsive_engines": [],
            },
        )

    adapter = SearXNGNameSearchAdapter(
        httpx.MockTransport(handler), base_url="http://searxng.test"
    )
    command = adapter.build_command("example_person", QueryType.USERNAME, tmp_path, 90)
    result = await adapter.execute(command, tmp_path, 90, "example_person")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example_person", QueryType.USERNAME, "raw")

    assert result.returncode == 0
    assert len(seen_queries) == 8
    assert all(not query.startswith("!") for query in seen_queries)
    assert len(records) == 1
    assert len(findings) == 1
    assert findings[0].query_type == QueryType.USERNAME
    assert findings[0].canonical_url == "https://forum.example.org/u/example_person"
    payload = json.loads((tmp_path / "searxng-name-search.json").read_text(encoding="utf-8"))
    assert payload["query_type"] == "username"
    assert payload["rejected_irrelevant_count"] == 15


@pytest.mark.asyncio
async def test_searxng_username_search_retains_instagram_hit_from_targeted_query(
    tmp_path: Path,
) -> None:
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        request_query = params["q"][0]
        seen_queries.append(request_query)
        if request_query.endswith('"sexdepts" site:instagram.com'):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.instagram.com/sexdepts",
                            "title": "sexdepts • Instagram photos and videos",
                            "content": "Public Instagram profile",
                            "engines": ["bing"],
                            "positions": [1],
                            "score": 1.0,
                        }
                    ],
                    "unresponsive_engines": [],
                },
            )
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    adapter = SearXNGNameSearchAdapter(
        httpx.MockTransport(handler), base_url="http://searxng.test"
    )
    command = adapter.build_command("sexdepts", QueryType.USERNAME, tmp_path, 90)
    result = await adapter.execute(command, tmp_path, 90, "sexdepts")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "sexdepts", QueryType.USERNAME, "raw")

    assert result.returncode == 0
    assert len(seen_queries) == 8
    assert any(query.endswith('"sexdepts" site:instagram.com') for query in seen_queries)
    assert len(findings) == 1
    assert findings[0].canonical_url == "https://www.instagram.com/sexdepts"
    assert findings[0].metadata["search_engines"] == ["bing"]

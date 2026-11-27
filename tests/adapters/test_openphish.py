from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.adapters import openphish
from app.adapters.openphish import OpenPhishAdapter
from app.models.schemas import QueryType


@pytest.mark.asyncio
async def test_openphish_correlates_domain_without_contacting_target(monkeypatch, tmp_path: Path) -> None:
    feed_url = "https://feeds.openphish.com/gov/example-feed.txt"
    monkeypatch.setattr(openphish, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path / "data"))
    monkeypatch.setattr(openphish, "get_openphish_feed_url", lambda _db: feed_url)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert str(request.url) == feed_url
        return httpx.Response(200, text="https://login.example.org/a\nhttps://unrelated.test/b\nhttp://example.org/c\n")

    adapter = OpenPhishAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "example.org")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example.org", QueryType.DOMAIN, "openphish.json")
    assert requested == [feed_url]
    assert len(findings) == 2
    assert all(item.finding_type == "known_phishing_url" for item in findings)
    assert all(item.canonical_url is None for item in findings)
    assert findings[0].metadata["source_scope"] == "passive_authorised_feed_correlation"


@pytest.mark.asyncio
async def test_openphish_uses_fresh_local_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(openphish, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path / "data"))
    monkeypatch.setattr(openphish, "get_openphish_feed_url", lambda _db: "https://feeds.openphish.com/gov/example-feed.txt")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="https://example.org/phish\n")

    adapter = OpenPhishAdapter(httpx.MockTransport(handler))
    for output in (tmp_path / "one", tmp_path / "two"):
        command = adapter.build_command("example.org", QueryType.DOMAIN, output, 30)
        result = await adapter.execute(command, output, 30, "example.org")
        assert result.returncode == 0
    assert calls == 1

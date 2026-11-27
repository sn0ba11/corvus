from types import SimpleNamespace

from app.services.historical_urls import build_historical_url_view


def test_historical_url_view_merges_sources_dates_and_query_keys() -> None:
    investigation = SimpleNamespace(
        findings=[
            SimpleNamespace(
                finding_type="historical_url",
                extra={"otx_observed_at": "2026-07-01T10:00:00Z"},
                platform="AlienVault OTX",
                source_tool="otx_historical",
                canonical_url="https://example.com/admin?user=alice",
                value=None,
                display_url=None,
            ),
            SimpleNamespace(
                finding_type="historical_url",
                extra={"observed_at": "2026-08-01T10:00:00Z"},
                platform="URLScan",
                source_tool="urlscan_historical",
                canonical_url="https://example.com/admin?user=alice&utm_source=x",
                value=None,
                display_url=None,
            ),
            SimpleNamespace(
                finding_type="profile",
                extra={
                    "wayback": {
                        "target_url": "https://example.com/profile",
                        "cdx_records": [
                            {
                                "captured_at": "2025-01-01T00:00:00Z",
                                "archive_url": "https://web.archive.org/example",
                            }
                        ],
                    }
                },
                platform="Example",
                source_tool="test",
                canonical_url="https://example.com/profile",
                value=None,
                display_url=None,
            ),
        ]
    )

    view = build_historical_url_view(investigation)
    assert view["enabled"] is True
    assert view["total_url_count"] == 2
    admin = next(item for item in view["urls"] if "/admin" in item["url"])
    assert admin["sources"] == ["AlienVault OTX", "URLScan"]
    assert admin["first_observed_at"] == "2026-07-01T10:00:00Z"
    assert admin["last_observed_at"] == "2026-08-01T10:00:00Z"
    assert view["query_keys"] == [{"key": "user", "count": 2}]
    assert view["source_counts"]["Wayback Machine"] == 1

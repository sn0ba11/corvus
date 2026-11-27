import json
from pathlib import Path

import httpx
import pytest

from app.adapters.site_intelligence import (
    LiveSiteIntelligenceAdapter,
    _extract_js_endpoints,
    _extract_tracking_ids,
    _parse_sitemap,
)
from app.models.schemas import FindingStatus, QueryType


def test_sitemap_parser_is_bounded_to_public_in_scope_urls() -> None:
    assert _parse_sitemap(
        "<urlset><url><loc>https://example.org/a</loc></url><url><loc>https://other.example/b</loc></url></urlset>",
        "example.org",
    ) == ["https://example.org/a"]


def test_tracking_id_extraction_is_typed_and_deduplicated() -> None:
    records = _extract_tracking_ids("UA-12345-1 G-ABCDEF12 GTM-ABCDE1 ca-pub-1234567890123456 UA-12345-1")
    assert records == [
        {"type": "google_analytics_legacy", "value": "UA-12345-1"},
        {"type": "google_analytics_ga4", "value": "G-ABCDEF12"},
        {"type": "google_tag_manager", "value": "GTM-ABCDE1"},
        {"type": "google_adsense_publisher", "value": "ca-pub-1234567890123456"},
    ]


def test_js_endpoint_extraction_rejects_assets_and_foreign_hosts() -> None:
    endpoints = _extract_js_endpoints(
        "fetch('/api/v1/users'); const a='/assets/app.css'; const b='https://evil.example/api/x'; const c='/graphql';",
        "https://example.org/assets/app.js",
        "example.org",
    )
    assert endpoints == ["https://example.org/api/v1/users", "https://example.org/graphql"]


@pytest.mark.asyncio
async def test_live_site_intelligence_crawls_bounded_same_domain_and_retains_source_provenance(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset><url><loc>https://example.org/sitemap-only</loc></url></urlset>", headers={"content-type": "application/xml"})
        if path == "/":
            return httpx.Response(
                200,
                text="""<html><head><meta name='generator' content='ExampleCMS 2.0'><script src='/assets/app.js'></script></head>
                <body>Contact ops@example.org
                <a href='/about'>About</a><a href='/private'>Private</a><a href='/docs/report.pdf'>Report</a>
                <a href='https://github.com/example'>GitHub</a><a href='https://partner.example.net/page'>Partner</a>
                <script>gtag('config','G-ABCDEF12')</script></body></html>""",
                headers={"content-type": "text/html; charset=utf-8", "server": "ExampleServer"},
            )
        if path in {"/about", "/sitemap-only"}:
            return httpx.Response(200, text="<html><body><a href='/'>Home</a></body></html>", headers={"content-type": "text/html"})
        if path == "/assets/app.js":
            return httpx.Response(200, text="fetch('/api/v2/profile'); const t='GTM-ABCDE1';\n//# sourceMappingURL=app.js.map", headers={"content-type": "application/javascript"})
        if path == "/assets/app.js.map":
            return httpx.Response(
                200,
                json={
                    "version": 3,
                    "sourceRoot": "webpack:///",
                    "sources": ["./node_modules/@scope/pkg/index.js"],
                    "sourcesContent": [
                        "// https://github.com/example/project.git\n// dev@example.org\nconst api='/internal/api';"
                    ],
                    "names": [],
                    "mappings": "",
                },
                headers={"content-type": "application/json"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    adapter = LiveSiteIntelligenceAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, 60)
    result = await adapter.execute(command, tmp_path, 60, "example.org")

    assert result.returncode == 0
    assert not any("/private" in url and not url.endswith("robots.txt") for url in requests)
    payload = json.loads((tmp_path / "site-intelligence.json").read_text(encoding="utf-8"))
    assert payload["robots"]["loaded"] is True
    assert payload["robots"]["disallowed_skipped"] == 1
    assert payload["sitemap_seed_count"] == 1
    assert any(page["url"] == "https://example.org/sitemap-only" for page in payload["pages"])
    assert payload["emails"] == [{"value": "ops@example.org", "source_url": "https://example.org/"}]
    assert payload["documents"] == [{"url": "https://example.org/docs/report.pdf", "source_url": "https://example.org/"}]
    assert payload["social_links"] == [{"url": "https://github.com/example", "source_url": "https://example.org/"}]
    assert payload["external_domains"][0]["domain"] == "partner.example.net"
    assert payload["javascript_endpoints"] == [{"url": "https://example.org/api/v2/profile", "script_url": "https://example.org/assets/app.js", "source_url": "https://example.org/"}]
    assert {item["value"] for item in payload["tracking_ids"]} == {"G-ABCDEF12", "GTM-ABCDE1"}
    assert {item["value"] for item in payload["technology_hints"]} == {"ExampleCMS 2.0", "ExampleServer"}
    assert payload["source_maps"][0]["map_url"] == "https://example.org/assets/app.js.map"
    assert payload["source_maps"][0]["status"] == "parsed"
    assert payload["source_maps"][0]["sources"][0]["package_names"] == ["@scope/pkg"]
    assert payload["source_maps"][0]["sources"][0]["emails"] == ["dev@example.org"]

    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "example.org", QueryType.DOMAIN, "raw/ref")
    by_type = {finding.finding_type: finding for finding in findings}
    assert by_type["email"].status == FindingStatus.FOUND
    assert by_type["document_url"].metadata["source_scope"] == "link_only_not_downloaded"
    assert by_type["tracking_identifier"].description is not None
    assert by_type["javascript_endpoint"].metadata["script_url"] == "https://example.org/assets/app.js"
    assert by_type["source_map_reference"].metadata["generated_asset"] == "https://example.org/assets/app.js"
    assert by_type["source_map_package_reference"].value == "@scope/pkg"
    assert by_type["source_map_developer_identifier"].value == "dev@example.org"
    assert by_type["source_map_endpoint"].canonical_url == "https://example.org/internal/api"
    assert all(finding.raw_output_reference == "raw/ref" for finding in findings)

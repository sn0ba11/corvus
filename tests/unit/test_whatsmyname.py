import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.services import egress_policy, whatsmyname
from app.services.whatsmyname import DatasetSummary, WhatsMyNameError


def dataset(*names: str) -> dict[str, object]:
    return {
        "license": ["CC-BY-SA-4.0"],
        "authors": ["WebBreacher"],
        "categories": ["tech"],
        "sites": [
            {
                "name": name,
                "uri_check": f"https://{name.casefold()}.example/{{account}}",
                "e_code": 200,
                "e_string": "profile",
                "m_code": 404,
                "m_string": "missing",
                "cat": "tech",
                "protection": [],
            }
            for name in names
        ],
    }


def test_store_and_load_dataset_with_restrictive_metadata(tmp_path: Path) -> None:
    payload = dataset("One")
    summary = DatasetSummary(
        revision="a" * 40,
        retrieved_at="2026-08-02T00:00:00+00:00",
        sha256="b" * 64,
        site_count=1,
        category_count=1,
        categories={"tech": 1},
        protected_site_count=0,
        schema_issue_count=0,
        source_url="https://example.test/wmn-data.json",
    )
    whatsmyname.store_dataset(tmp_path, payload, summary)
    loaded = whatsmyname.cached_dataset(tmp_path)
    assert loaded == (payload, summary)
    assert (tmp_path / whatsmyname.WMN_CACHE_NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_coverage_status_reports_added_removed_and_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pinned = dataset("One", "Two")
    active = dataset("Two", "Three")
    active["sites"][0]["e_string"] = "changed"  # type: ignore[index]

    async def fake_fetch(revision: str) -> tuple[dict[str, object], DatasetSummary]:
        summary = DatasetSummary(
            revision=revision,
            retrieved_at="2026-08-02T00:00:00+00:00",
            sha256=revision[:1] * 64,
            site_count=2,
            category_count=1,
            categories={"tech": 2},
            protected_site_count=0,
            schema_issue_count=0,
            source_url="https://example.test/wmn-data.json",
        )
        return pinned, summary

    monkeypatch.setattr(whatsmyname, "commit_for", lambda _: "a" * 40)
    monkeypatch.setattr(whatsmyname, "fetch_dataset", fake_fetch)
    whatsmyname.store_dataset(
        tmp_path,
        active,
        replace((await fake_fetch("b" * 40))[1], revision="b" * 40),
    )
    comparison = await whatsmyname.coverage_status(tmp_path)
    assert comparison.added_sites == ("three",)
    assert comparison.removed_sites == ("one",)
    assert comparison.changed_sites == ("two",)


@pytest.mark.asyncio
async def test_download_json_rejects_oversized_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1025)

    def fake_async_client(*, transport: object = None, **kwargs: object) -> httpx.AsyncClient:
        del transport
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(egress_policy.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(WhatsMyNameError, match="size limit"):
        await whatsmyname._download_json("https://example.test/data.json", max_bytes=1024)


def test_invalid_dataset_is_rejected() -> None:
    with pytest.raises(WhatsMyNameError):
        whatsmyname._validate_dataset({"sites": [], "categories": []})
    with pytest.raises(WhatsMyNameError):
        whatsmyname._validate_dataset(json.loads('{"sites": "bad", "categories": []}'))

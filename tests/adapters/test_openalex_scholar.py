import json
from pathlib import Path

import httpx
import pytest

from app.adapters.openalex_scholar import OpenAlexScholarAdapter, parse_openalex_authors
from app.models.schemas import FindingStatus, QueryType


def test_openalex_parser_retains_identity_affiliation_topics_and_orcid() -> None:
    records = parse_openalex_authors(
        {
            "results": [
                {
                    "id": "https://openalex.org/A1234567890",
                    "display_name": "Example Researcher",
                    "display_name_alternatives": ["E Researcher"],
                    "orcid": "https://orcid.org/0000-0002-1825-0097",
                    "works_count": 42,
                    "cited_by_count": 100,
                    "last_known_institutions": [
                        {"display_name": "Example University"},
                    ],
                    "affiliations": [
                        {"institution": {"display_name": "Earlier Institute"}},
                    ],
                    "topics": [{"display_name": "Digital Forensics"}],
                }
            ]
        },
        "Example Researcher",
    )

    assert records[0]["openalex_id"] == "A1234567890"
    assert records[0]["exact_name"] is True
    assert records[0]["institutions"] == ["Example University", "Earlier Institute"]
    assert records[0]["topics"] == ["Digital Forensics"]


@pytest.mark.asyncio
async def test_openalex_scholar_collects_bounded_public_author_candidates(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.openalex.org"
        assert request.url.path == "/authors"
        assert request.url.params["search"] == "Example Researcher"
        assert request.url.params["per-page"] == "8"
        return httpx.Response(
            200,
            json={
                "meta": {"count": 1},
                "results": [
                    {
                        "id": "https://openalex.org/A1234567890",
                        "display_name": "Example Researcher",
                        "display_name_alternatives": ["E Researcher"],
                        "orcid": "https://orcid.org/0000-0002-1825-0097",
                        "works_count": 42,
                        "cited_by_count": 100,
                        "last_known_institutions": [
                            {"display_name": "Example University"},
                        ],
                        "topics": [{"display_name": "Digital Forensics"}],
                    }
                ],
            },
        )

    adapter = OpenAlexScholarAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(
        adapter.build_command("Example Researcher", QueryType.NAME, tmp_path, 30),
        tmp_path,
        30,
        "Example Researcher",
    )

    assert result.returncode == 0
    assert len(seen) == 1
    payload = json.loads((tmp_path / "openalex-scholar.json").read_text(encoding="utf-8"))
    assert payload["results"][0]["openalex_id"] == "A1234567890"

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "Example Researcher",
        QueryType.NAME,
        "raw/openalex.json",
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == "scholarly_profile"
    assert finding.status == FindingStatus.LIKELY
    assert finding.confidence == 0.72
    assert finding.profile.display_name == "Example Researcher"
    assert finding.profile.location == "Example University"
    assert finding.metadata["orcid"] == "https://orcid.org/0000-0002-1825-0097"
    assert "corroborate" in finding.metadata["identity_caution"].casefold()


def test_openalex_scholar_rejects_non_name_query(tmp_path: Path) -> None:
    adapter = OpenAlexScholarAdapter()
    with pytest.raises(ValueError, match="full-name"):
        adapter.build_command("example", QueryType.USERNAME, tmp_path, 30)

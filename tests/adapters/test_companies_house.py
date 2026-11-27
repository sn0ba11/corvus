from pathlib import Path

import httpx
import pytest

from app.adapters import companies_house
from app.adapters.companies_house import CompaniesHouseAdapter
from app.models.schemas import QueryType


@pytest.mark.asyncio
async def test_companies_house_collects_company_officer_and_appointments(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(companies_house, "get_companies_house_api_key", lambda _db: "test_key_1234567890")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        if request.url.path == "/search/companies":
            return httpx.Response(200, json={"items": [{"company_number": "12345678", "title": "EXAMPLE LIMITED", "company_status": "active", "company_type": "ltd", "address": {"locality": "Belfast", "postal_code": "BT1 1AA"}}]})
        if request.url.path == "/search/officers":
            return httpx.Response(200, json={"items": [{"title": "SMITH, JOHN", "appointment_count": 1, "date_of_birth": {"month": 5, "year": 1980}, "links": {"self": "/officers/officer-1/appointments"}}]})
        if request.url.path == "/officers/officer-1/appointments":
            return httpx.Response(200, json={"items": [{"appointed_to": {"company_number": "12345678", "company_name": "EXAMPLE LIMITED"}, "officer_role": "director", "appointed_on": "2020-01-02"}]})
        raise AssertionError(str(request.url))

    adapter = CompaniesHouseAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("John Smith", QueryType.NAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "John Smith")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "John Smith", QueryType.NAME, "companies-house.json")
    assert result.returncode == 0
    assert {finding.finding_type for finding in findings} == {"legal_entity", "companies_house_officer", "company_officer_appointment"}
    appointment = next(item for item in findings if item.finding_type == "company_officer_appointment")
    assert appointment.metadata["officer_role"] == "director"
    assert appointment.metadata["company_number"] == "12345678"


@pytest.mark.asyncio
async def test_companies_house_requires_configured_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(companies_house, "get_companies_house_api_key", lambda _db: None)
    adapter = CompaniesHouseAdapter()
    command = adapter.build_command("John Smith", QueryType.NAME, tmp_path, 45)
    result = await adapter.execute(command, tmp_path, 45, "John Smith")
    assert result.error_code == "configuration_required"

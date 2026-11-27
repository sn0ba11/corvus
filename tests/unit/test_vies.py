import httpx
import pytest

from app.services.vies import VIESError, check_vat_number, normalise_vat_number


def test_vies_normalises_supported_vat_numbers() -> None:
    assert normalise_vat_number("IE 6388047V") == ("IE6388047V", "IE", "6388047V")
    assert normalise_vat_number("GR-123456789") == ("EL123456789", "EL", "123456789")
    with pytest.raises(VIESError):
        normalise_vat_number("GB123456789")


@pytest.mark.asyncio
async def test_vies_rest_validation_retains_official_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rest-api/check-vat-number")
        assert request.method == "POST"
        assert request.content == b'{"countryCode":"IE","vatNumber":"6388047V"}'
        return httpx.Response(200, json={"countryCode": "IE", "vatNumber": "6388047V", "requestDate": "2026-09-11T06:00:00Z", "valid": True, "requestIdentifier": "abc123", "name": "EXAMPLE LIMITED", "address": "DUBLIN"})

    result = await check_vat_number("IE6388047V", httpx.MockTransport(handler))
    assert result.valid is True
    assert result.name == "EXAMPLE LIMITED"
    assert result.request_identifier == "abc123"

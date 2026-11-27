from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

ENDPOINT = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
MAX_RESPONSE_BYTES = 512 * 1024
VAT_RE = re.compile(r"^([A-Z]{2})([A-Z0-9]{2,14})$")
VIES_CODES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI",
}


class VIESError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VIESResult:
    vat_number: str
    country_code: str
    number: str
    valid: bool
    request_date: str | None
    request_identifier: str | None
    name: str | None
    address: str | None
    trader_company_type: str | None
    raw: dict[str, Any]


def normalise_vat_number(value: str) -> tuple[str, str, str]:
    compact = re.sub(r"[\s.\-]", "", value.strip().upper())
    if compact.startswith("GR"):
        compact = "EL" + compact[2:]
    match = VAT_RE.fullmatch(compact)
    if not match or match.group(1) not in VIES_CODES:
        raise VIESError("enter an EU/Northern Ireland VAT number including its two-letter prefix, for example IE6388047V")
    country, number = match.groups()
    return compact, country, number


async def check_vat_number(value: str, transport: httpx.AsyncBaseTransport | None = None) -> VIESResult:
    canonical, country, number = normalise_vat_number(value)
    client_context = (
        httpx.AsyncClient(transport=transport, timeout=20.0, follow_redirects=False, trust_env=False, headers={"User-Agent": "Corvus/0.1 VIES validation"})
        if transport is not None
        else safe_client(timeout=20.0, user_agent="Corvus/0.1 VIES validation")
    )
    try:
        async with client_context as client:
            response = await client.send(
                client.build_request(
                    "POST",
                    ENDPOINT,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"countryCode": country, "vatNumber": number},
                ),
                stream=True,
            )
            try:
                raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
            finally:
                await response.aclose()
    except (httpx.HTTPError, EgressPolicyError) as exc:
        raise VIESError("VIES validation request failed") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VIESError("VIES returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise VIESError("VIES returned an unexpected response")
    if response.status_code >= 400:
        wrappers_raw = payload.get("errorWrappers")
        wrappers: list[Any] = wrappers_raw if isinstance(wrappers_raw, list) else []
        detail = next((str(item.get("message") or item.get("error")) for item in wrappers if isinstance(item, dict) and (item.get("message") or item.get("error"))), "VIES could not process the request")
        raise VIESError(detail[:300])
    valid = payload.get("valid")
    if not isinstance(valid, bool):
        raise VIESError("VIES response did not include a validation result")

    def clean(key: str, limit: int = 1000) -> str | None:
        item = payload.get(key)
        if item in (None, "", "---"):
            return None
        text = " ".join(str(item).split())[:limit]
        return text or None

    return VIESResult(
        vat_number=canonical,
        country_code=country,
        number=number,
        valid=valid,
        request_date=clean("requestDate", 100),
        request_identifier=clean("requestIdentifier", 200),
        name=clean("name", 300) or clean("traderName", 300),
        address=clean("address", 1000) or ", ".join(filter(None, (clean("traderStreet", 300), clean("traderPostalCode", 100), clean("traderCity", 200)))) or None,
        trader_company_type=clean("traderCompanyType", 200),
        raw=payload,
    )

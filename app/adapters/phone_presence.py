from __future__ import annotations

import asyncio
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client
from app.services.phone_intelligence import PhoneNormalisationError, normalise_phone
from app.services.phone_presence_settings import (
    DEFAULT_PLATFORM_IDS,
    get_active_phone_presence_platforms,
)
from app.services.runtime_settings import DEFAULT_RUNTIME_OPTIONS, get_runtime_options

MAX_RESPONSE_BYTES = 256 * 1024
USER_AGENT = "Corvus/0.1 Phone presence collector"

SNAPCHAT_ACCOUNTS_URL = "https://accounts.snapchat.com"
SNAPCHAT_VALIDATE_URL = "https://accounts.snapchat.com/accounts/validate_phone_number"

# Independent reimplementation of the sign-in-form probe documented by
# megadose/ignorant. See app/services/phone_presence_settings.py for why this
# platform defaults to disabled.
AMAZON_SIGNIN_GET_URL = (
    "https://www.amazon.com/ap/signin"
    "?openid.pape.max_auth_age=0"
    "&openid.return_to=https%3A%2F%2Fwww.amazon.com%2F%3F_encoding%3DUTF8%26ref_%3Dnav_ya_signin"
    "&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.assoc_handle=usflex"
    "&openid.mode=checkid_setup"
    "&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select"
    "&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"
)
AMAZON_SIGNIN_POST_URL = "https://www.amazon.com/ap/signin/"
AMAZON_ACCOUNT_RECOGNISED_ELEMENT_ID = "auth-password-missing-alert"

PLATFORM_DOMAINS: dict[str, str] = {
    "snapchat": "snapchat.com",
    "amazon": "amazon.com",
}

# ISO 3166-1 alpha-2 country codes for the international calling codes this
# deployment already recognises (see CALLING_CODE_REGION in phone_intelligence.py).
# Snapchat's public phone-registration endpoint expects an ISO-2 region rather
# than a calling code, so this is a narrow companion lookup for that one call.
CALLING_CODE_ISO2: dict[str, str] = {
    "1": "US", "20": "EG", "27": "ZA", "30": "GR", "31": "NL", "32": "BE", "33": "FR",
    "34": "ES", "36": "HU", "39": "IT", "40": "RO", "41": "CH", "43": "AT", "44": "GB",
    "45": "DK", "46": "SE", "47": "NO", "48": "PL", "49": "DE", "51": "PE", "52": "MX",
    "53": "CU", "54": "AR", "55": "BR", "56": "CL", "57": "CO", "58": "VE", "60": "MY",
    "61": "AU", "62": "ID", "63": "PH", "64": "NZ", "65": "SG", "66": "TH", "81": "JP",
    "82": "KR", "84": "VN", "86": "CN", "90": "TR", "91": "IN", "92": "PK", "93": "AF",
    "94": "LK", "95": "MM", "98": "IR", "211": "SS", "212": "MA", "213": "DZ", "216": "TN",
    "218": "LY", "220": "GM", "221": "SN", "222": "MR", "223": "ML", "224": "GN",
    "225": "CI", "226": "BF", "227": "NE", "228": "TG", "229": "BJ", "230": "MU",
    "231": "LR", "232": "SL", "233": "GH", "234": "NG", "235": "TD", "236": "CF",
    "237": "CM", "238": "CV", "239": "ST", "240": "GQ", "241": "GA", "242": "CG",
    "243": "CD", "244": "AO", "245": "GW", "246": "IO", "248": "SC", "249": "SD",
    "250": "RW", "251": "ET", "252": "SO", "253": "DJ", "254": "KE", "255": "TZ",
    "256": "UG", "257": "BI", "258": "MZ", "260": "ZM", "261": "MG", "262": "RE",
    "263": "ZW", "264": "NA", "265": "MW", "266": "LS", "267": "BW", "268": "SZ",
    "269": "KM", "290": "SH", "291": "ER", "297": "AW", "298": "FO", "299": "GL",
    "350": "GI", "351": "PT", "352": "LU", "353": "IE", "354": "IS", "355": "AL",
    "356": "MT", "357": "CY", "358": "FI", "359": "BG", "370": "LT", "371": "LV",
    "372": "EE", "373": "MD", "374": "AM", "375": "BY", "376": "AD", "377": "MC",
    "378": "SM", "380": "UA", "381": "RS", "382": "ME", "385": "HR", "386": "SI",
    "387": "BA", "389": "MK", "420": "CZ", "421": "SK", "423": "LI", "500": "FK",
    "501": "BZ", "502": "GT", "503": "SV", "504": "HN", "505": "NI", "506": "CR",
    "507": "PA", "508": "PM", "509": "HT", "590": "GP", "591": "BO", "592": "GY",
    "593": "EC", "594": "GF", "595": "PY", "596": "MQ", "597": "SR", "598": "UY",
    "599": "CW", "670": "TL", "673": "BN", "674": "NR", "675": "PG", "676": "TO",
    "677": "SB", "678": "VU", "679": "FJ", "680": "PW", "681": "WF", "682": "CK",
    "683": "NU", "685": "WS", "686": "KI", "687": "NC", "688": "TV", "689": "PF",
    "690": "TK", "691": "FM", "692": "MH", "850": "KP", "852": "HK", "853": "MO",
    "855": "KH", "856": "LA", "880": "BD", "886": "TW", "960": "MV", "961": "LB",
    "962": "JO", "963": "SY", "964": "IQ", "965": "KW", "966": "SA", "967": "YE",
    "968": "OM", "970": "PS", "971": "AE", "972": "IL", "973": "BH", "974": "QA",
    "975": "BT", "976": "MN", "977": "NP", "992": "TJ", "993": "TM", "994": "AZ",
    "995": "GE", "996": "KG", "998": "UZ",
}


class _HiddenInputParser(HTMLParser):
    """Collects an HTML form's hidden ``<input name=... value=...>`` fields."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "input":
            return
        values = {key.casefold(): value for key, value in attrs if value is not None}
        name = values.get("name")
        if name and name not in self.fields:
            self.fields[name] = values.get("value", "")


class _ElementIdPresenceParser(HTMLParser):
    """Detects whether one specific element ``id`` appears anywhere in a page."""

    def __init__(self, target_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target_id = target_id
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.found:
            return
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if values.get("id") == self.target_id:
            self.found = True


def _empty_result(name: str, error: str, *, skipped: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "domain": PLATFORM_DOMAINS[name],
        "exists": None,
        "rate_limited": False,
        "error": error,
        # A benign, expected non-attempt (e.g. an unsupported region) rather than a
        # failed request. Skipped checks still count as a successful collector run.
        "skipped": skipped,
    }


class PhonePresenceAdapter(Adapter):
    """Checks selected public signup or sign-in surfaces for phone presence.

    Snapchat is enabled by default when the collector is selected. Amazon
    also requires an explicit platform opt-in. Responses can change or be
    challenged, so only recognised responses become findings.
    """

    tool_id = "phone_presence"
    display_name = "Phone Number Presence (Snapchat/Amazon)"
    executable = "corvus-phone-presence"
    capabilities = frozenset({"phone_presence_check"})
    supported_query_types = frozenset({QueryType.PHONE})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport
        # Set once per execute() call; exists so tests can observe the routing
        # decision (case proxy vs. Tor vs. none) without needing a real network.
        self.last_effective_proxy: str | None = None

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "Snapchat/Amazon phone-presence checks")

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.PHONE:
            raise ValueError("phone presence check accepts phone investigations only")
        normalise_phone(query)
        command = [
            self.executable,
            "--number",
            query,
            "--output",
            str(output_dir / "phone-presence.json"),
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    def _client(self, proxy: str | None, timeout: float) -> httpx.AsyncClient:
        if self.transport is not None:
            return httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT},
            )
        return safe_client(timeout=timeout, user_agent=USER_AGENT, proxy=proxy)

    async def _check_snapchat(
        self, calling_code: str, national_number: str, proxy: str | None, timeout: float
    ) -> dict[str, Any]:
        iso2 = CALLING_CODE_ISO2.get(calling_code)
        if iso2 is None:
            return _empty_result(
                "snapchat", "no ISO-3166 region mapping for this calling code", skipped=True
            )
        try:
            async with self._client(proxy, timeout) as client:
                landing = await client.get(SNAPCHAT_ACCOUNTS_URL)
                token = landing.cookies.get("xsrf_token")
                if not token:
                    return _empty_result("snapchat", "did not receive an xsrf token")
                response = await client.post(
                    SNAPCHAT_VALIDATE_URL,
                    data={
                        "phone_country_code": iso2,
                        "phone_number": national_number,
                        "xsrf_token": token,
                    },
                    headers={"Origin": SNAPCHAT_ACCOUNTS_URL},
                )
                if response.status_code == 429:
                    return {**_empty_result("snapchat", "rate limited"), "rate_limited": True}
                response.raise_for_status()
                raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
            payload = json.loads(raw)
            status_code = payload.get("status_code") if isinstance(payload, dict) else None
        except (EgressPolicyError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return _empty_result("snapchat", str(exc)[:300])
        exists = True if status_code == "TAKEN_NUMBER" else False if status_code == "OK" else None
        return {
            "name": "snapchat",
            "domain": "snapchat.com",
            "exists": exists,
            "rate_limited": False,
            "error": None,
            "skipped": False,
        }

    async def _check_amazon(
        self, calling_code: str, national_number: str, proxy: str | None, timeout: float
    ) -> dict[str, Any]:
        try:
            async with self._client(proxy, timeout) as client:
                landing = await client.get(
                    AMAZON_SIGNIN_GET_URL, headers={"Accept-Language": "en-US,en;q=0.9"}
                )
                if landing.status_code == 429:
                    return {**_empty_result("amazon", "rate limited"), "rate_limited": True}
                landing.raise_for_status()
                landing_html = await read_bounded_response(landing, max_bytes=MAX_RESPONSE_BYTES)
                form_parser = _HiddenInputParser()
                form_parser.feed(landing_html.decode("utf-8", errors="replace"))
                if not form_parser.fields:
                    return _empty_result("amazon", "sign-in form was not in the expected shape")
                form_fields = dict(form_parser.fields)
                form_fields["email"] = f"{calling_code}{national_number}"
                response = await client.post(AMAZON_SIGNIN_POST_URL, data=form_fields)
                if response.status_code == 429:
                    return {**_empty_result("amazon", "rate limited"), "rate_limited": True}
                response.raise_for_status()
                result_html = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
        except (EgressPolicyError, httpx.HTTPError, ValueError) as exc:
            return _empty_result("amazon", str(exc)[:300])
        alert_parser = _ElementIdPresenceParser(AMAZON_ACCOUNT_RECOGNISED_ELEMENT_ID)
        alert_parser.feed(result_html.decode("utf-8", errors="replace"))
        return {
            "name": "amazon",
            "domain": "amazon.com",
            "exists": alert_parser.found,
            "rate_limited": False,
            "error": None,
            "skipped": False,
        }

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = normalise_phone(query)
        except PhoneNormalisationError as exc:
            return ExecutionResult(
                2, "", str(exc), error_code="invalid_phone_number", error_message=str(exc)
            )
        expected = [
            self.executable,
            "--number",
            query,
            "--output",
            str(output_dir / "phone-presence.json"),
        ]
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        if proxy:
            expected.extend(["--proxy", proxy])
        if command != expected:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="phone presence command did not match the bounded adapter contract",
            )
        try:
            with SessionLocal() as db:
                enabled_platforms = get_active_phone_presence_platforms(db)
                runtime_options = get_runtime_options(db)
        except SQLAlchemyError:
            enabled_platforms = DEFAULT_PLATFORM_IDS
            runtime_options = dict(DEFAULT_RUNTIME_OPTIONS)
        if not enabled_platforms:
            return ExecutionResult(
                0,
                json.dumps({"status": "no_platforms_enabled"}),
                "",
                error_code="no_platforms_enabled",
                error_message="no phone-presence platforms are enabled in Settings",
            )
        # An explicit per-case proxy always wins. Otherwise, if the examiner has
        # switched on Tor routing for this collector in Settings and a Tor proxy
        # is actually configured, use it; a missing configuration falls back to
        # the deployment's normal egress rather than erroring.
        tor_proxy = get_settings().tor_proxy if runtime_options.get("phone_presence_via_tor") else None
        effective_proxy = proxy or tor_proxy or None
        self.last_effective_proxy = effective_proxy
        calling_code = metadata.calling_code.lstrip("+")
        checks: dict[str, Any] = {}
        if "snapchat" in enabled_platforms:
            checks["snapchat"] = self._check_snapchat
        if "amazon" in enabled_platforms:
            checks["amazon"] = self._check_amazon
        request_timeout = min(15.0, max(5.0, float(timeout) / max(1, len(checks))))
        try:
            async with asyncio.timeout(timeout):
                results = await asyncio.gather(
                    *(
                        check(calling_code, metadata.national_number, effective_proxy, request_timeout)
                        for check in checks.values()
                    )
                )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"phone presence checks exceeded {timeout}s",
            )
        records = list(results)
        non_failures = [
            record for record in records if record.get("error") is None or record.get("skipped")
        ]
        rate_limited = any(record.get("rate_limited") for record in records)
        if not non_failures:
            return ExecutionResult(
                None,
                "",
                "",
                rate_limited=rate_limited,
                error_code="rate_limited" if rate_limited else "upstream_error",
                error_message="every enabled phone-presence check failed",
            )
        path = output_dir / "phone-presence.json"
        path.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        if log:
            summary = ", ".join(f"{r['name']}={r.get('exists')}" for r in records)
            await log("stdout", f"Phone presence checks completed: {summary}")
        return ExecutionResult(
            0,
            json.dumps({"status": "completed", "platforms": sorted(checks)}),
            "",
            output_files=[path],
            rate_limited=rate_limited,
        )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "phone-presence.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if query_type != QueryType.PHONE:
            return []
        findings: list[FindingRecord] = []
        for record in records:
            if record.get("exists") is not True:
                continue
            name = str(record.get("name") or "platform")
            platform_label = name.title()
            domain = str(record.get("domain") or PLATFORM_DOMAINS.get(name, ""))
            findings.append(
                FindingRecord(
                    query_type=QueryType.PHONE,
                    queried_value=query,
                    finding_type="phone_platform_presence",
                    platform=platform_label,
                    value=f"{platform_label} account registered to this number",
                    status=FindingStatus.LIKELY,
                    confidence=0.55,
                    title=f"Phone number registered on {platform_label}",
                    description=(
                        f"{domain}'s public signup/sign-in surface reports this number as already "
                        "registered. This indicates registration, not account content or identity, "
                        "and has not been independently corroborated."
                    ),
                    metadata={
                        "phone_presence_domain": domain,
                        "phone_presence_platform": name,
                    },
                    source_tool=self.tool_id,
                    source_record_id=f"{name}:{query}",
                    raw_output_reference=raw_reference,
                )
            )
        return findings

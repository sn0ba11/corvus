from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client
from app.services.external_api_settings import get_openphish_feed_url

MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_MATCHES = 100
CACHE_TTL = timedelta(minutes=10)
USER_AGENT = "Corvus/0.1 OpenPhish authorised-feed correlation"


def _domain(value: str) -> str:
    target = value.strip().casefold().rstrip(".")
    if not target or "/" in target or ":" in target or "." not in target or len(target) > 253:
        raise ValueError("OpenPhish correlation requires a bare domain")
    return target


def _host_matches(url: str, domain: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host == domain or host.endswith(f".{domain}")


def _extract_feed_urls(raw: bytes) -> list[str]:
    text = raw.decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    urls: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        candidate = value.strip()
        if candidate.startswith(("http://", "https://")) and len(candidate) <= 4096:
            urls.append(candidate)

    if stripped.startswith(("[", "{")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    add(item.get("url") or item.get("URL") or item.get("phishing_url"))
        elif isinstance(payload, dict):
            for key in ("urls", "feed", "results", "data"):
                values = payload.get(key)
                if not isinstance(values, list):
                    continue
                for item in values:
                    if isinstance(item, str):
                        add(item)
                    elif isinstance(item, dict):
                        add(item.get("url") or item.get("URL") or item.get("phishing_url"))
    if not urls and "," in text.splitlines()[0] if text.splitlines() else False:
        try:
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                add(row.get("url") or row.get("URL") or row.get("phishing_url"))
        except (csv.Error, TypeError):
            pass
    if not urls:
        for line in text.splitlines():
            add(line)
    return list(dict.fromkeys(urls))


class OpenPhishAdapter(Adapter):
    tool_id = "openphish"
    display_name = "OpenPhish Authorised Feed"
    executable = "openphish-correlation"
    capabilities = frozenset({"phishing_feed_correlation", "domain_reputation", "local_feed_cache"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        with SessionLocal() as db:
            configured = get_openphish_feed_url(db) is not None
        return HealthResult(
            configured,
            "ready" if configured else "authorised feed URL not configured",
            "OpenPhish licensed/Gov feed",
        )

    def build_command(
        self,
        query: str,
        query_type: QueryType,
        output_dir: Path,
        timeout: int,
        proxy: str | None = None,
    ) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("OpenPhish accepts domain investigations only")
        domain = _domain(query)
        command = [self.executable, "--domain", domain, "--output", str(output_dir / "openphish.json")]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _load_feed(
        self,
        feed_url: str,
        proxy: str | None,
        timeout: float,
    ) -> tuple[list[str], dict[str, Any]]:
        cache_dir = get_settings().data_dir / "cache" / "openphish"
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        feed_path = cache_dir / "authorised-feed.txt"
        metadata_path = cache_dir / "metadata.json"
        source_hash = hashlib.sha256(feed_url.encode()).hexdigest()
        now = datetime.now(UTC)
        metadata: dict[str, Any] = {}
        if metadata_path.is_file() and feed_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                retrieved = datetime.fromisoformat(str(metadata.get("retrieved_at")))
                if retrieved.tzinfo is None:
                    retrieved = retrieved.replace(tzinfo=UTC)
                if (
                    metadata.get("source_url_sha256") == source_hash
                    and now - retrieved <= CACHE_TTL
                ):
                    lines = [
                        line.strip()
                        for line in feed_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    return lines, metadata
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                metadata = {}
        client_context = (
            httpx.AsyncClient(
                transport=self.transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT},
            )
            if self.transport is not None
            else safe_client(timeout=timeout, user_agent=USER_AGENT, proxy=proxy)
        )
        async with client_context as client:
            response = await client.send(
                client.build_request(
                    "GET",
                    feed_url,
                    headers={"Accept": "text/plain,text/csv,application/json;q=0.9,*/*;q=0.1"},
                ),
                stream=True,
            )
            try:
                if response.status_code == 429:
                    raise RuntimeError("rate_limited")
                response.raise_for_status()
                raw = await read_bounded_response(response, max_bytes=MAX_FEED_BYTES)
            finally:
                await response.aclose()
        urls = _extract_feed_urls(raw)
        feed_path.write_text("\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")
        feed_path.chmod(0o600)
        metadata = {
            "retrieved_at": now.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "entry_count": len(urls),
            "source_url_sha256": source_hash,
            "feed": "OpenPhish authorised feed",
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        metadata_path.chmod(0o600)
        return urls, metadata

    async def execute(
        self,
        command: list[str],
        output_dir: Path,
        timeout: int,
        query: str,
        log: LogCallback | None = None,
    ) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        domain = _domain(query)
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        expected = [self.executable, "--domain", domain, "--output", str(output_dir / "openphish.json")]
        if proxy:
            expected.extend(["--proxy", proxy])
        if command != expected:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="invalid_command",
                error_message="OpenPhish command did not match the bounded adapter contract",
            )
        with SessionLocal() as db:
            feed_url = get_openphish_feed_url(db)
        if feed_url is None:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="configuration_required",
                error_message="Configure an authorised OpenPhish feed URL in Settings",
            )
        try:
            async with asyncio.timeout(timeout):
                feed, metadata = await self._load_feed(
                    feed_url,
                    proxy,
                    min(20.0, max(5.0, float(timeout))),
                )
            matches = [url for url in feed if _host_matches(url, domain)][:MAX_MATCHES]
            artifact = {
                "source": "OpenPhish authorised feed",
                "domain": domain,
                "checked_at": datetime.now(UTC).isoformat(),
                "feed_retrieved_at": metadata.get("retrieved_at"),
                "feed_sha256": metadata.get("sha256"),
                "feed_entry_count": metadata.get("entry_count"),
                "matches": matches,
                "methodology": (
                    "Exact host or subdomain correlation against a locally cached, operator-authorised "
                    "OpenPhish feed; no target URL was contacted."
                ),
            }
            path = output_dir / "openphish.json"
            path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"OpenPhish correlation found {len(matches)} matching feed URL(s)")
            return ExecutionResult(
                0,
                json.dumps({"status": "found" if matches else "not_found", "matches": len(matches)}),
                "",
                output_files=[path],
            )
        except TimeoutError:
            return ExecutionResult(
                None,
                "",
                "",
                timed_out=True,
                error_code="timeout",
                error_message=f"OpenPhish correlation exceeded {timeout}s",
            )
        except RuntimeError as exc:
            if str(exc) == "rate_limited":
                return ExecutionResult(
                    None,
                    "",
                    "",
                    rate_limited=True,
                    error_code="rate_limited",
                    error_message="OpenPhish feed source rate limited the request",
                )
            raise
        except (httpx.HTTPError, EgressPolicyError, OSError, ValueError) as exc:
            return ExecutionResult(
                None,
                "",
                "",
                error_code="upstream_error",
                error_message=f"OpenPhish correlation failed: {exc}",
            )

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "openphish.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(
        self,
        records: list[dict[str, Any]],
        query: str,
        query_type: QueryType,
        raw_reference: str | None,
    ) -> list[FindingRecord]:
        if not records:
            return []
        payload = records[0]
        domain = _domain(query)
        findings: list[FindingRecord] = []
        matches_raw = payload.get("matches")
        matches: list[Any] = matches_raw if isinstance(matches_raw, list) else []
        for index, value in enumerate(matches[:MAX_MATCHES]):
            url = str(value)
            findings.append(
                FindingRecord(
                    query_type=query_type,
                    queried_value=domain,
                    finding_type="known_phishing_url",
                    platform="OpenPhish",
                    value=url,
                    status=FindingStatus.FOUND,
                    confidence=0.98,
                    title=f"OpenPhish feed match for {domain}",
                    description=(
                        "The URL appears in the operator-authorised OpenPhish feed. Corvus did not "
                        "browse or execute the phishing URL."
                    ),
                    metadata={
                        "phishing_url": url,
                        "feed_retrieved_at": payload.get("feed_retrieved_at"),
                        "feed_sha256": payload.get("feed_sha256"),
                        "source_scope": "passive_authorised_feed_correlation",
                    },
                    source_tool=self.tool_id,
                    source_record_id=(
                        f"openphish:{hashlib.sha256(url.encode()).hexdigest()[:24]}:{index}"
                    ),
                    raw_output_reference=raw_reference,
                )
            )
        return findings

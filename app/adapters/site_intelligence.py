from __future__ import annotations

import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring as safe_xml_fromstring  # type: ignore[import-untyped]

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.adapters.bing_name import _public_result_url
from app.core.security import canonicalise_url
from app.models.schemas import FindingRecord, FindingStatus, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

MAX_PAGES = 100
MAX_DEPTH = 2
MAX_HTML_BYTES = 768 * 1024
MAX_JS_FILES = 24
MAX_JS_BYTES = 512 * 1024
MAX_SOURCE_MAPS = 8
MAX_SOURCE_MAP_BYTES = 1024 * 1024
MAX_SOURCE_MAP_SOURCES = 100
MAX_SOURCE_CONTENT_CHARS = 32 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_RESULTS_PER_KIND = 250
USER_AGENT = "Corvus/0.1 bounded live-site intelligence"
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".json", ".xml", ".txt", ".rtf"
}
SOCIAL_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "github.com", "gitlab.com", "mastodon.social", "threads.net"
}
EMAIL_RE = re.compile(r"(?<![A-Z0-9._%+-])([A-Z0-9._%+-]{1,64}@[A-Z0-9.-]{1,253}\.[A-Z]{2,63})(?![A-Z0-9._%+-])", re.I)
TRACKING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_analytics_legacy", re.compile(r"\bUA-\d{4,12}-\d{1,4}\b", re.I)),
    ("google_analytics_ga4", re.compile(r"\bG-[A-Z0-9]{6,20}\b", re.I)),
    ("google_tag_manager", re.compile(r"\bGTM-[A-Z0-9]{5,20}\b", re.I)),
    ("google_ads", re.compile(r"\bAW-\d{5,20}\b", re.I)),
    ("google_adsense_publisher", re.compile(r"\bca-pub-\d{6,30}\b", re.I)),
)
JS_ENDPOINT_RE = re.compile(r"(?P<q>['\"])(?P<url>(?:https?://[^'\"\s]+|/[A-Za-z0-9_~!$&()*+,;=:@%./?\-]{2,500}))(?P=q)")
SOURCE_MAP_RE = re.compile(
    r"(?:\/\/[#@]\s*sourceMappingURL=(?P<line>[^\s]+)|\/\*[#@]\s*sourceMappingURL=(?P<block>[^\s*]+)\s*\*\/)",
    re.I,
)
REPOSITORY_RE = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitlab\.com)/[A-Za-z0-9_.~-]+/[A-Za-z0-9_.~-]+(?:\.git)?",
    re.I,
)
PACKAGE_PATH_RE = re.compile(r"(?:^|[/\\])node_modules[/\\]((?:@[^/\\]+[/\\])?[^/\\]+)", re.I)
ASSET_SUFFIXES = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map")


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.scripts: list[str] = []
        self.mailto: list[str] = []
        self.generator: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        if tag.casefold() in {"a", "area", "link"} and values.get("href"):
            href = str(values["href"]).strip()
            self.links.append(href)
            if href.casefold().startswith("mailto:"):
                self.mailto.append(href[7:].split("?", 1)[0])
        if tag.casefold() == "script" and values.get("src"):
            self.scripts.append(str(values["src"]).strip())
        if tag.casefold() == "meta" and values.get("name", "").casefold() == "generator":
            self.generator = str(values.get("content") or "").strip()[:200] or None


def _target_domain(value: str) -> str:
    target = value.strip().casefold().rstrip(".")
    if not target or "/" in target or ":" in target or len(target) > 253:
        raise ValueError("live-site intelligence requires a bare domain")
    labels = target.split(".")
    if len(labels) < 2 or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid domain")
    return target


def _in_scope(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return host == domain or host.endswith(f".{domain}")


def _normalise_link(base_url: str, value: str) -> str | None:
    text = value.strip()
    if not text or text.startswith(("#", "javascript:", "data:", "tel:")):
        return None
    if text.casefold().startswith("mailto:"):
        return None
    return _public_result_url(urljoin(base_url, text))


def _document_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return any(path.endswith(extension) for extension in DOCUMENT_EXTENSIONS)


def _social_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    return any(host == candidate or host.endswith(f".{candidate}") for candidate in SOCIAL_HOSTS)


def _parse_sitemap(xml_text: str, domain: str) -> list[str]:
    try:
        root = safe_xml_fromstring(xml_text, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise ValueError("sitemap contained a prohibited XML declaration") from exc
    except ET.ParseError as exc:
        raise ValueError("sitemap was not valid XML") from exc
    urls: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() != "loc" or not element.text:
            continue
        candidate = _public_result_url(element.text.strip())
        if not candidate or not _in_scope(candidate, domain) or candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= MAX_PAGES:
            break
    return urls


def _extract_tracking_ids(text: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for tracking_type, pattern in TRACKING_PATTERNS:
        for match in pattern.findall(text):
            value = str(match)
            key = (tracking_type, value.casefold())
            if key in seen:
                continue
            seen.add(key)
            results.append({"type": tracking_type, "value": value})
    return results[:MAX_RESULTS_PER_KIND]


def _extract_js_endpoints(text: str, script_url: str, domain: str) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for match in JS_ENDPOINT_RE.finditer(text):
        raw = match.group("url")
        candidate = _normalise_link(script_url, raw)
        if not candidate or not _in_scope(candidate, domain):
            continue
        path = urlsplit(candidate).path.casefold()
        if path.endswith(ASSET_SUFFIXES) or path in {"", "/"}:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        results.append(candidate)
        if len(results) >= MAX_RESULTS_PER_KIND:
            break
    return results


def _source_mapping_reference(text: str, headers: httpx.Headers | None = None) -> str | None:
    if headers is not None:
        header_value = headers.get("sourcemap") or headers.get("x-sourcemap")
        if header_value:
            return header_value.strip()[:2048] or None
    matches = list(SOURCE_MAP_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    value = match.group("line") or match.group("block")
    return value.strip().strip("'\"")[:2048] or None


def _extract_source_map_record(
    raw: bytes,
    map_url: str,
    generated_asset: str,
    source_page: str,
    domain: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "map_url": map_url,
        "generated_asset": generated_asset,
        "source_page": source_page,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "parsed",
        "source_root": None,
        "sources": [],
    }
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        record["status"] = "malformed_json"
        return record
    if not isinstance(payload, dict):
        record["status"] = "malformed_structure"
        return record
    if isinstance(payload.get("sections"), list):
        record["status"] = "indexed_map_unsupported"
        return record
    sources = payload.get("sources")
    if not isinstance(sources, list):
        record["status"] = "missing_sources"
        return record
    source_root = payload.get("sourceRoot")
    record["source_root"] = str(source_root)[:2048] if isinstance(source_root, str) else None
    source_contents = payload.get("sourcesContent")
    contents = source_contents if isinstance(source_contents, list) else []
    for index, raw_path in enumerate(sources[:MAX_SOURCE_MAP_SOURCES]):
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        source_path = raw_path.strip()[:2048]
        content = contents[index] if index < len(contents) and isinstance(contents[index], str) else ""
        content = content[:MAX_SOURCE_CONTENT_CHARS]
        repositories = sorted(set(REPOSITORY_RE.findall(f"{source_path}\n{content}")))[:20]
        package_names = sorted(
            {
                match.group(1).replace("\\", "/")
                for match in PACKAGE_PATH_RE.finditer(source_path)
                if match.group(1)
            }
        )[:20]
        emails = sorted(
            {
                email.strip().casefold().rstrip(".,;:")
                for email in EMAIL_RE.findall(content)
                if len(email.strip()) <= 320
            }
        )[:20]
        endpoints = _extract_js_endpoints(content, map_url, domain)[:20] if content else []
        record["sources"].append(
            {
                "path": source_path,
                "repository_urls": repositories,
                "package_names": package_names,
                "emails": emails,
                "endpoints": endpoints,
            }
        )
    return record


def _append_unique(bucket: list[dict[str, Any]], seen: set[str], key: str, record: dict[str, Any]) -> None:
    if key in seen or len(bucket) >= MAX_RESULTS_PER_KIND:
        return
    seen.add(key)
    bucket.append(record)


class LiveSiteIntelligenceAdapter(Adapter):
    tool_id = "site_intelligence"
    display_name = "Live Site Intelligence"
    executable = "live-site-intelligence"
    capabilities = frozenset({"bounded_crawl", "document_discovery", "email_discovery", "social_link_discovery", "javascript_endpoint_discovery", "tracking_id_discovery", "technology_hints", "explicit_source_map_analysis"})
    supported_query_types = frozenset({QueryType.DOMAIN})
    default_selected = False
    default_timeout = 120
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "native bounded crawler")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type != QueryType.DOMAIN:
            raise ValueError("Live Site Intelligence accepts domain investigations only")
        domain = _target_domain(query)
        command = [self.executable, domain, "--output", str(output_dir / "site-intelligence.json"), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def _request(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> tuple[httpx.Response, bytes]:
        request = client.build_request("GET", url, headers={"Accept": "text/html,application/xhtml+xml,application/javascript,text/javascript;q=0.8,*/*;q=0.1"})
        response = await client.send(request, stream=True)
        try:
            raw = await read_bounded_response(response, max_bytes=max_bytes)
        finally:
            await response.aclose()
        return response, raw

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(command) < 6 or command[0] != self.executable or command[1] != query.strip().casefold().rstrip("."):
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="Live Site Intelligence command did not match the bounded adapter contract")
        domain = _target_domain(query)
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        started_at = datetime.now(UTC)
        pages: list[dict[str, Any]] = []
        emails: list[dict[str, Any]] = []
        documents: list[dict[str, Any]] = []
        social_links: list[dict[str, Any]] = []
        external_domains: list[dict[str, Any]] = []
        endpoints: list[dict[str, Any]] = []
        tracking_ids: list[dict[str, Any]] = []
        technology_hints: list[dict[str, Any]] = []
        source_maps: list[dict[str, Any]] = []
        seen_source_maps: set[str] = set()
        seen_by_kind: dict[str, set[str]] = {name: set() for name in ("emails", "documents", "social", "external", "endpoints", "tracking", "technology")}
        queued: set[str] = set()
        visited: set[str] = set()
        scripts: dict[str, str] = {}
        total_bytes = 0
        robots = RobotFileParser()
        robots_loaded = False
        robots_disallowed = 0
        request_timeout = min(20.0, max(5.0, float(timeout) / 3))
        client_context = httpx.AsyncClient(transport=self.transport, timeout=request_timeout, follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT}) if self.transport is not None else safe_client(timeout=request_timeout, user_agent=USER_AGENT, proxy=proxy)
        try:
            async with asyncio.timeout(timeout):
                async with client_context as client:
                    root = f"https://{domain}/"
                    robots_url = f"https://{domain}/robots.txt"
                    try:
                        response, raw = await self._request(client, robots_url, 256 * 1024)
                        if response.status_code == 200:
                            robots.parse(raw.decode(response.encoding or "utf-8", errors="replace").splitlines())
                            robots.set_url(robots_url)
                            robots_loaded = True
                            total_bytes += len(raw)
                    except (httpx.HTTPError, EgressPolicyError):
                        pass
                    sitemap_urls = list(robots.site_maps() or [])[:3] if robots_loaded else []
                    default_sitemap = f"https://{domain}/sitemap.xml"
                    if default_sitemap not in sitemap_urls:
                        sitemap_urls.append(default_sitemap)
                    sitemap_seeds: list[str] = []
                    for sitemap_url in sitemap_urls[:3]:
                        candidate_sitemap = _public_result_url(sitemap_url)
                        if not candidate_sitemap or not _in_scope(candidate_sitemap, domain):
                            continue
                        try:
                            sitemap_response, sitemap_raw = await self._request(client, candidate_sitemap, 512 * 1024)
                        except (httpx.HTTPError, EgressPolicyError):
                            continue
                        if sitemap_response.status_code != 200:
                            continue
                        total_bytes += len(sitemap_raw)
                        try:
                            sitemap_seeds.extend(
                                _parse_sitemap(
                                    sitemap_raw.decode(sitemap_response.encoding or "utf-8", errors="replace"),
                                    domain,
                                )
                            )
                        except ValueError:
                            continue
                    queue: deque[tuple[str, int]] = deque([(root, 0)])
                    queued.add(root)
                    for seed in sitemap_seeds[:MAX_PAGES]:
                        if seed not in queued:
                            queue.append((seed, 1))
                            queued.add(seed)
                    while queue and len(visited) < MAX_PAGES and total_bytes < MAX_TOTAL_BYTES:
                        url, depth = queue.popleft()
                        if url in visited:
                            continue
                        if robots_loaded and not robots.can_fetch(USER_AGENT, url):
                            robots_disallowed += 1
                            continue
                        visited.add(url)
                        try:
                            response, raw = await self._request(client, url, MAX_HTML_BYTES)
                        except (httpx.HTTPError, EgressPolicyError) as exc:
                            pages.append({"url": url, "depth": depth, "status": "error", "error": str(exc)[:300]})
                            continue
                        total_bytes += len(raw)
                        content_type = response.headers.get("content-type", "").casefold()
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "")
                            redirected = _normalise_link(url, location)
                            if redirected and _in_scope(redirected, domain) and redirected not in queued:
                                queue.appendleft((redirected, depth))
                                queued.add(redirected)
                            pages.append({"url": url, "depth": depth, "status": response.status_code, "redirect_to": redirected})
                            continue
                        pages.append({"url": url, "depth": depth, "status": response.status_code, "content_type": content_type[:200], "bytes": len(raw)})
                        if response.status_code != 200 or "html" not in content_type:
                            continue
                        text = raw.decode(response.encoding or "utf-8", errors="replace")
                        parser = _PageParser()
                        parser.feed(text)
                        for email in parser.mailto + EMAIL_RE.findall(text):
                            normalised = email.strip().casefold().rstrip(".,;:")
                            if len(normalised) <= 320:
                                _append_unique(emails, seen_by_kind["emails"], normalised, {"value": normalised, "source_url": url})
                        for item in _extract_tracking_ids(text):
                            key = f"{item['type']}:{item['value'].casefold()}"
                            _append_unique(tracking_ids, seen_by_kind["tracking"], key, {**item, "source_url": url})
                        if parser.generator:
                            key = f"generator:{parser.generator.casefold()}"
                            _append_unique(technology_hints, seen_by_kind["technology"], key, {"type": "html_generator", "value": parser.generator, "source_url": url})
                        for header_name in ("server", "x-powered-by"):
                            value = response.headers.get(header_name)
                            if value:
                                key = f"{header_name}:{value.casefold()}"
                                _append_unique(technology_hints, seen_by_kind["technology"], key, {"type": header_name, "value": value[:200], "source_url": url})
                        for raw_link in parser.links:
                            candidate = _normalise_link(url, raw_link)
                            if not candidate:
                                continue
                            if _in_scope(candidate, domain):
                                if _document_url(candidate):
                                    _append_unique(documents, seen_by_kind["documents"], candidate, {"url": candidate, "source_url": url})
                                elif depth < MAX_DEPTH and candidate not in queued and len(queued) < MAX_PAGES * 4:
                                    queue.append((candidate, depth + 1))
                                    queued.add(candidate)
                            else:
                                host = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
                                if _social_url(candidate):
                                    _append_unique(social_links, seen_by_kind["social"], candidate, {"url": candidate, "source_url": url})
                                elif host:
                                    _append_unique(external_domains, seen_by_kind["external"], host, {"domain": host, "source_url": url, "url": candidate})
                        for raw_script in parser.scripts:
                            candidate = _normalise_link(url, raw_script)
                            if candidate and _in_scope(candidate, domain) and len(scripts) < MAX_JS_FILES:
                                scripts.setdefault(candidate, url)
                    for script_url, source_page in list(scripts.items())[:MAX_JS_FILES]:
                        if total_bytes >= MAX_TOTAL_BYTES:
                            break
                        try:
                            response, raw = await self._request(client, script_url, MAX_JS_BYTES)
                        except (httpx.HTTPError, EgressPolicyError):
                            continue
                        if response.status_code != 200:
                            continue
                        total_bytes += len(raw)
                        text = raw.decode(response.encoding or "utf-8", errors="replace")
                        for endpoint in _extract_js_endpoints(text, script_url, domain):
                            _append_unique(endpoints, seen_by_kind["endpoints"], endpoint, {"url": endpoint, "script_url": script_url, "source_url": source_page})
                        for item in _extract_tracking_ids(text):
                            key = f"{item['type']}:{item['value'].casefold()}"
                            _append_unique(tracking_ids, seen_by_kind["tracking"], key, {**item, "source_url": script_url})
                        map_reference = _source_mapping_reference(text, response.headers)
                        if map_reference and len(source_maps) < MAX_SOURCE_MAPS and total_bytes < MAX_TOTAL_BYTES:
                            map_url = _normalise_link(script_url, map_reference)
                            if map_url and _in_scope(map_url, domain) and map_url not in seen_source_maps:
                                seen_source_maps.add(map_url)
                                try:
                                    map_response, map_raw = await self._request(client, map_url, MAX_SOURCE_MAP_BYTES)
                                except (httpx.HTTPError, EgressPolicyError):
                                    source_maps.append(
                                        {
                                            "map_url": map_url,
                                            "generated_asset": script_url,
                                            "source_page": source_page,
                                            "status": "request_failed",
                                            "sources": [],
                                        }
                                    )
                                else:
                                    if map_response.status_code == 200:
                                        total_bytes += len(map_raw)
                                        source_maps.append(
                                            _extract_source_map_record(
                                                map_raw,
                                                map_url,
                                                script_url,
                                                source_page,
                                                domain,
                                            )
                                        )
                                    else:
                                        source_maps.append(
                                            {
                                                "map_url": map_url,
                                                "generated_asset": script_url,
                                                "source_page": source_page,
                                                "status": f"http_{map_response.status_code}",
                                                "sources": [],
                                            }
                                        )
            payload = {
                "schema_version": "1.0",
                "source": "Corvus native bounded live-site intelligence",
                "queried_domain": domain,
                "started_at": started_at.isoformat(),
                "limits": {"max_pages": MAX_PAGES, "max_depth": MAX_DEPTH, "max_js_files": MAX_JS_FILES, "max_source_maps": MAX_SOURCE_MAPS, "max_source_map_bytes": MAX_SOURCE_MAP_BYTES, "max_source_map_sources": MAX_SOURCE_MAP_SOURCES, "max_total_bytes": MAX_TOTAL_BYTES},
                "robots": {"loaded": robots_loaded, "disallowed_skipped": robots_disallowed},
                "sitemap_seed_count": len(sitemap_seeds),
                "pages": pages,
                "emails": emails,
                "documents": documents,
                "social_links": social_links,
                "external_domains": external_domains,
                "javascript_endpoints": endpoints,
                "tracking_ids": tracking_ids,
                "technology_hints": technology_hints,
                "source_maps": source_maps,
                "bytes_read": total_bytes,
            }
            retained_count = (
                len(emails)
                + len(documents)
                + len(social_links)
                + len(external_domains)
                + len(endpoints)
                + len(tracking_ids)
                + len(technology_hints)
                + len(source_maps)
            )
            path = output_dir / "site-intelligence.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            if log:
                await log("stdout", f"Live Site Intelligence crawled {len(pages)} page response(s), {len(scripts)} JavaScript file(s), and retained {retained_count} extracted observation(s)")
            return ExecutionResult(0, json.dumps({"status": "found", "pages": len(pages), "bytes_read": total_bytes}), "", output_files=[path])
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"Live Site Intelligence exceeded {timeout}s")
        except (httpx.HTTPError, EgressPolicyError, ValueError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"Live Site Intelligence failed: {exc}")

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "site-intelligence.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType, raw_reference: str | None) -> list[FindingRecord]:
        if not records:
            return []
        payload = records[0]
        findings: list[FindingRecord] = []
        domain = _target_domain(query)

        def add(kind: str, platform: str, value: str, source_record_id: str, *, url: str | None = None, confidence: float = 0.8, metadata: dict[str, Any] | None = None, description: str | None = None) -> None:
            findings.append(FindingRecord(query_type=query_type, queried_value=domain, finding_type=kind, platform=platform, value=value, canonical_url=canonicalise_url(url) if url else None, display_url=url, status=FindingStatus.FOUND, confidence=confidence, title=value, description=description, metadata=metadata or {}, source_tool=self.tool_id, source_record_id=source_record_id[:300], raw_output_reference=raw_reference))

        for item in payload.get("emails", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("value"):
                value = str(item["value"])
                add("email", "Live Site", value, f"email:{value}", confidence=0.85, metadata={"source_url": item.get("source_url"), "source_scope": "public_page_observation"})
        for item in payload.get("documents", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("url"):
                url = str(item["url"])
                add("document_url", "Live Site", url, f"document:{url}", url=url, confidence=0.9, metadata={"source_url": item.get("source_url"), "source_scope": "link_only_not_downloaded"})
        for item in payload.get("social_links", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("url"):
                url = str(item["url"])
                add("linked_social_profile", "Live Site", url, f"social:{url}", url=url, confidence=0.7, metadata={"source_url": item.get("source_url"), "source_scope": "site_link_no_identity_assertion"})
        for item in payload.get("external_domains", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("domain"):
                value = str(item["domain"])
                add("external_domain", "Live Site", value, f"domain:{value}", confidence=0.6, metadata={"source_url": item.get("source_url"), "linked_url": item.get("url"), "source_scope": "outbound_link_not_crawled"})
        for item in payload.get("javascript_endpoints", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("url"):
                url = str(item["url"])
                add("javascript_endpoint", "Live Site", url, f"endpoint:{url}", url=url, confidence=0.65, metadata={"source_url": item.get("source_url"), "script_url": item.get("script_url"), "source_scope": "static_javascript_string_observation"})
        for item in payload.get("tracking_ids", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("type") and item.get("value"):
                tracking_type = str(item["type"])
                value = str(item["value"])
                add("tracking_identifier", "Live Site", value, f"tracking:{tracking_type}:{value}", confidence=0.9, metadata={"tracking_type": tracking_type, "source_url": item.get("source_url"), "source_scope": "public_source_identifier"}, description="Tracking identifiers can support cross-site correlation but do not by themselves prove common ownership.")
        for item in payload.get("technology_hints", [])[:MAX_RESULTS_PER_KIND]:
            if isinstance(item, dict) and item.get("type") and item.get("value"):
                hint_type = str(item["type"])
                value = str(item["value"])
                add("technology_hint", "Live Site", value, f"technology:{hint_type}:{value}", confidence=0.7, metadata={"hint_type": hint_type, "source_url": item.get("source_url"), "source_scope": "declared_header_or_generator"})
        source_observations = 0
        for item in payload.get("source_maps", [])[:MAX_SOURCE_MAPS]:
            if not isinstance(item, dict) or not item.get("map_url"):
                continue
            map_url = str(item["map_url"])
            generated_asset = str(item.get("generated_asset") or "")
            map_status = str(item.get("status") or "unknown")
            add(
                "source_map_reference",
                "Live Site",
                map_url,
                f"source-map:{map_url}",
                url=map_url,
                confidence=0.95 if map_status == "parsed" else 0.75,
                metadata={
                    "generated_asset": generated_asset,
                    "source_page": item.get("source_page"),
                    "map_status": map_status,
                    "bytes": item.get("bytes"),
                    "sha256": item.get("sha256"),
                    "source_root": item.get("source_root"),
                    "source_scope": "explicit_source_map_reference",
                },
                description="The generated public asset explicitly referenced this source map; extracted paths and endpoints are leads, not proof that internal-looking resources are live.",
            )
            if map_status != "parsed":
                continue
            for source in item.get("sources", [])[:MAX_SOURCE_MAP_SOURCES]:
                if not isinstance(source, dict) or source_observations >= MAX_RESULTS_PER_KIND:
                    continue
                source_path = str(source.get("path") or "")[:2048]
                if source_path:
                    add(
                        "source_map_source_path",
                        "Live Site",
                        source_path,
                        f"source-map-path:{map_url}:{source_path}",
                        confidence=0.7,
                        metadata={
                            "map_url": map_url,
                            "generated_asset": generated_asset,
                            "source_scope": "deployed_public_source_map_path",
                        },
                        description="A path declared by a deployed public source map; it may describe build-time structure rather than live infrastructure.",
                    )
                    source_observations += 1
                for repository_url in source.get("repository_urls", [])[:20]:
                    if source_observations >= MAX_RESULTS_PER_KIND:
                        break
                    repository = str(repository_url)
                    add(
                        "source_map_repository_reference",
                        "Live Site",
                        repository,
                        f"source-map-repository:{map_url}:{repository}",
                        url=repository,
                        confidence=0.75,
                        metadata={"map_url": map_url, "generated_asset": generated_asset, "source_path": source_path},
                    )
                    source_observations += 1
                for package_name in source.get("package_names", [])[:20]:
                    if source_observations >= MAX_RESULTS_PER_KIND:
                        break
                    package = str(package_name)
                    add(
                        "source_map_package_reference",
                        "Live Site",
                        package,
                        f"source-map-package:{map_url}:{package}",
                        confidence=0.7,
                        metadata={"map_url": map_url, "generated_asset": generated_asset, "source_path": source_path},
                    )
                    source_observations += 1
                for email in source.get("emails", [])[:20]:
                    if source_observations >= MAX_RESULTS_PER_KIND:
                        break
                    value = str(email)
                    add(
                        "source_map_developer_identifier",
                        "Live Site",
                        value,
                        f"source-map-email:{map_url}:{value}",
                        confidence=0.65,
                        metadata={"map_url": map_url, "generated_asset": generated_asset, "source_path": source_path, "source_scope": "public_source_content_identifier"},
                        description="Identifier observed in deployed source-map content; treat it as a correlation lead rather than proof of repository or account ownership.",
                    )
                    source_observations += 1
                for endpoint in source.get("endpoints", [])[:20]:
                    if source_observations >= MAX_RESULTS_PER_KIND:
                        break
                    url = str(endpoint)
                    add(
                        "source_map_endpoint",
                        "Live Site",
                        url,
                        f"source-map-endpoint:{map_url}:{url}",
                        url=url,
                        confidence=0.65,
                        metadata={"map_url": map_url, "generated_asset": generated_asset, "source_path": source_path, "source_scope": "static_source_map_string_observation"},
                    )
                    source_observations += 1
        return findings

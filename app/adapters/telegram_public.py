from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import read_bounded_response, safe_client
from app.services.telegram_intelligence import PUBLIC_USERNAME_RE

MAX_RESPONSE_BYTES = 768 * 1024
USER_AGENT = "Corvus/0.1 Telegram public-page collector"


class _TelegramPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.in_title = False
        self.extra_parts: list[str] = []
        self.in_extra = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs if value is not None}
        tag = tag.casefold()
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.meta[key.casefold()] = content.strip()
        elif tag in {"div", "span"} and "tgme_page_extra" in values.get("class", ""):
            self.in_extra = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False
        if tag.casefold() in {"div", "span"} and self.in_extra:
            self.in_extra = False

    def handle_data(self, data: str) -> None:
        cleaned = " ".join(data.split())
        if self.in_title and cleaned:
            self.title_parts.append(cleaned)
        if self.in_extra and cleaned:
            self.extra_parts.append(cleaned)

    def result(self, username: str) -> dict[str, Any]:
        title = self.meta.get("og:title") or " ".join(self.title_parts).strip() or username
        description = self.meta.get("og:description") or self.meta.get("description")
        image = self.meta.get("og:image") or self.meta.get("twitter:image")
        page_url = self.meta.get("og:url") or f"https://t.me/{username}"
        extra = " ".join(self.extra_parts).strip() or None
        generic = title.casefold().startswith(("telegram: contact", "telegram: join")) or bool(
            description and re.search(r"if you have telegram, you can (?:contact|view and join)", description, re.I)
        )
        canonical = urlsplit(page_url)
        matches = canonical.hostname == "t.me" and canonical.path.strip("/").casefold() == username.casefold()
        positive = not generic and matches and title != username and bool(description or image or extra)
        kind = "channel" if extra and "subscribers" in extra.casefold() else "group" if extra and "members" in extra.casefold() else "public_handle"
        count_match = re.search(r"([0-9][0-9 ,]*)\s+(?:subscribers|members)\b", extra or "", re.I)
        count = int(re.sub(r"\D", "", count_match[1])) if count_match else None
        return {
            "status": "found" if positive else "unknown",
            "username": username,
            "title": title[:500],
            "description": description[:3000] if description else None,
            "avatar_url": image[:2000] if image and image.startswith("https://") else None,
            "url": page_url[:2000] if page_url.startswith("https://t.me/") else f"https://t.me/{username}",
            "page_extra": extra[:500] if extra else None,
            "entity_kind": kind,
            "member_count": count,
            "limitation": None if positive else "No matching account-specific public profile evidence",
            "source_scope": "telegram_public_web_page",
            "retrieved_at": datetime.now(UTC).isoformat(),
        }


_POST_RE = re.compile(r'data-post="([A-Za-z0-9_]{1,40})/(\d{1,12})"')
_POST_TEXT_RE = re.compile(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_POST_TIME_RE = re.compile(r'<time[^>]*datetime="([0-9T:+\-]{10,40})"')
_HREF_RE = re.compile(r'href="(https?://[^"\s<>]{4,2000})"')
_COUNTER_RE = re.compile(r'class="counter_value">([^<]{1,20})</span>\s*<span class="counter_type">([a-z]{1,20})</span>')
MAX_PREVIEW_POSTS = 20


def parse_channel_preview(page: str, username: str) -> dict[str, Any]:
    """Extract the newest public posts, their dates and the outside links they share."""
    matches = [match for match in _POST_RE.finditer(page) if match[1].casefold() == username.casefold()]
    posts: list[dict[str, Any]] = []
    linked_sites: list[str] = []
    for index, match in enumerate(matches[-MAX_PREVIEW_POSTS:]):
        following = matches[-MAX_PREVIEW_POSTS:][index + 1].start() if index + 1 < len(matches[-MAX_PREVIEW_POSTS:]) else len(page)
        block = page[match.end():following]
        text_match = _POST_TEXT_RE.search(block)
        text = " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text_match[1])).split())[:600] if text_match else None
        time_match = _POST_TIME_RE.search(block)
        links = []
        for link in _HREF_RE.findall(text_match[1] if text_match else ""):
            link = html.unescape(link)
            host = (urlsplit(link).hostname or "").casefold()
            if host in {"t.me", "telegram.me", "telegram.org"} or host.endswith(".telegram.org"):
                continue
            links.append(link)
            if link not in linked_sites and len(linked_sites) < 20:
                linked_sites.append(link)
        posts.append({"post_id": int(match[2]), "url": f"https://t.me/{username}/{match[2]}",
                      "posted_at": time_match[1] if time_match else None, "text": text, "links": links[:10]})
    counters = {kind: value.strip() for value, kind in _COUNTER_RE.findall(page)[:8]}
    return {"recent_posts": posts, "linked_sites": linked_sites,
            "photo_count": counters.get("photos"), "video_count": counters.get("videos"),
            "link_count": counters.get("links")}


class TelegramPublicAdapter(Adapter):
    tool_id = "telegram_public"
    display_name = "Telegram Public Page"
    executable = "telegram-public-page"
    capabilities = frozenset({"username_search", "telegram_public_profile", "profile_image"})
    supported_query_types = frozenset({QueryType.USERNAME})
    default_selected = True
    platform_name = "Telegram"
    profile_url_template = "https://t.me/{username}"
    result_filename = "telegram-public.json"
    username_pattern = PUBLIC_USERNAME_RE
    page_parser = _TelegramPageParser
    # Channels expose a public post preview; subclasses for other providers switch this off.
    preview_url_template: str | None = "https://t.me/s/{username}"
    default_timeout = 30
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "public t.me HTML")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path, timeout: int, proxy: str | None = None) -> list[str]:
        if query_type != QueryType.USERNAME:
            raise ValueError("Public profile collection accepts username investigations only")
        username = query.strip().removeprefix("@").strip()
        # A handle this platform cannot hold is not an error in a multi-tool search: execute()
        # records it as unsupported without contacting the provider.
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", username):
            raise ValueError("Invalid public profile username")
        command = [self.executable, "--username", username, "--output", str(output_dir / self.result_filename), "--timeout", str(timeout)]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int, query: str, log: LogCallback | None = None) -> ExecutionResult:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        username = query.strip().removeprefix("@").strip()
        if len(command) < 7 or command[0] != self.executable or command[2] != username:
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="Public profile command did not match the bounded adapter contract")
        proxy = command[command.index("--proxy") + 1] if "--proxy" in command else None
        url = self.profile_url_template.format(username=quote(username, safe="._-"))
        def retain(record: dict[str, Any]) -> ExecutionResult:
            record = {"username": username, "url": url, **record}
            path = output_dir / self.result_filename
            path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
            path.chmod(0o600)
            return ExecutionResult(0, json.dumps({"status": record["status"], "username": username}), "", output_files=[path])
        if not self.username_pattern.fullmatch(username):
            return retain({"status": "unsupported_username",
                           "limitation": f"{self.platform_name} usernames cannot take this form; no request was made"})
        try:
            async with asyncio.timeout(timeout):
                client_context = httpx.AsyncClient(transport=self.transport, timeout=min(15.0, float(timeout)), follow_redirects=False, trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}) if self.transport is not None else safe_client(timeout=min(15.0, float(timeout)), user_agent=USER_AGENT, proxy=proxy)
                async with client_context as client:
                    response = await client.send(client.build_request("GET", url), stream=True)
                    try:
                        if response.status_code in {404, 410}:
                            return retain({"status": "not_found", "http_status": response.status_code})
                        if response.status_code in {401, 403}:
                            return ExecutionResult(None, "", "", error_code="access_restricted", error_message="Provider restricted the public page request")
                        if response.status_code == 429:
                            return ExecutionResult(None, "", "", rate_limited=True, error_code="rate_limited", error_message="Provider rate limited the public page request")
                        if response.is_redirect:
                            return ExecutionResult(None, "", "", error_code="redirected", error_message="Provider redirected the public page request")
                        response.raise_for_status()
                        if "text/html" not in response.headers.get("content-type", "").casefold():
                            raise ValueError("Provider returned a non-HTML public page")
                        raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
                    parser = self.page_parser()
                    parser.feed(raw.decode("utf-8", errors="replace"))
                    record = parser.result(username)
                    if self.preview_url_template and record.get("status") == "found" and record.get("entity_kind") == "channel":
                        record.update(await self._channel_preview(client, username))
            return retain(record)
        except TimeoutError:
            return ExecutionResult(None, "", "", timed_out=True, error_code="timeout", error_message=f"Public profile collection exceeded {timeout}s")
        except (httpx.HTTPError, ValueError) as exc:
            return ExecutionResult(None, "", "", error_code="upstream_error", error_message=f"Public profile collection failed: {exc}")

    async def _channel_preview(self, client: httpx.AsyncClient, username: str) -> dict[str, Any]:
        """Read the public post preview of a channel; a failure here never fails the lookup."""
        if not self.preview_url_template:
            return {}
        url = self.preview_url_template.format(username=quote(username, safe="_"))
        try:
            response = await client.send(client.build_request("GET", url), stream=True)
            try:
                if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").casefold():
                    return {"preview_state": f"unavailable (HTTP {response.status_code})"}
                raw = await read_bounded_response(response, max_bytes=MAX_RESPONSE_BYTES)
            finally:
                await response.aclose()
        except (httpx.HTTPError, ValueError):
            return {"preview_state": "unavailable"}
        return {"preview_state": "read", "preview_url": url,
                **parse_channel_preview(raw.decode("utf-8", errors="replace"), username)}

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / self.result_filename
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
        record = records[0]
        username = str(record.get("username") or "").strip()
        url = str(record.get("url") or "").strip()
        if not username or not url or record.get("status") == "unsupported_username":
            return []
        positive = record.get("status") == "found"
        status = FindingStatus.LIKELY if positive else FindingStatus.NOT_FOUND if record.get("status") == "not_found" else FindingStatus.UNKNOWN
        details = [{"label": "Telegram entity kind", "value": str(record.get("entity_kind", "public_handle"))}]
        if record.get("member_count") is not None:
            details.append({"label": "Subscribers" if record.get("entity_kind") == "channel" else "Members", "value": str(record["member_count"])})
        posts = [post for post in record.get("recent_posts", []) if isinstance(post, dict)] if positive else []
        if posts:
            details.append({"label": "Latest public post", "value": str(posts[-1].get("posted_at") or "date not shown")})
            details.append({"label": "Public posts read", "value": str(len(posts))})
        for label, key in (("Photos", "photo_count"), ("Videos", "video_count"), ("Links shared", "link_count")):
            if positive and record.get(key):
                details.append({"label": label, "value": str(record[key])})
        linked_sites = [link for link in record.get("linked_sites", []) if isinstance(link, str)] if positive else []
        details.extend({"label": "Link in a recent post", "value": link} for link in linked_sites[:10])
        return [FindingRecord(query_type=query_type, queried_value=query, finding_type="telegram_public_profile", platform="Telegram", value=username, canonical_url=url, display_url=url, status=status, confidence=0.78 if positive else 0.0, title=record.get("title") if positive else username, description=record.get("description") if positive else record.get("limitation"), profile=ProfileEvidence(avatar_url=record.get("avatar_url"), display_name=record.get("title"), biography=record.get("description"), account_id=username) if positive else ProfileEvidence(account_id=username), metadata={**record, "telegram_entity_kind": record.get("entity_kind", "public_handle"), "platform_details": details if positive else []}, source_tool=self.tool_id, source_record_id=username.casefold(), raw_output_reference=raw_reference)]

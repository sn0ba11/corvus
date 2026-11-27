import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx

from app.services.egress_policy import (
    EgressPayloadKind,
    EgressPolicyError,
    read_bounded_response,
    safe_client,
    validate_external_egress,
)
from app.services.image_cache import ImageCache, ImageCacheError
from app.services.image_correlation import hamming_distance, perceptual_dhash

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
SAVE_PAGE_ENDPOINT = "https://web.archive.org/save"
MAX_CAPTURES = 200
MAX_ARCHIVED_PAGE_BYTES = 1024 * 1024
MAX_CDX_RESPONSE_BYTES = 2 * 1024 * 1024
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,63}(?![\w.-])")


class WaybackError(RuntimeError):
    pass


class _ArchivedPageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.in_title = False
        self.description: str | None = None
        self.meta: dict[str, str] = {}
        self.links: set[str] = set()
        self.images: set[str] = set()
        self.avatar_candidates: list[str] = []
        self.text_parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value}
        if tag.lower() in {"script", "style", "noscript"}:
            self.ignored_depth += 1
        elif tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content")
            if name and content and len(name) <= 120 and len(content) <= 2000:
                self.meta.setdefault(name, content.strip())
            if name in {"description", "og:description"} and not self.description:
                self.description = content
            if name in {"og:image", "twitter:image", "twitter:image:src"} and content:
                self.avatar_candidates.append(urljoin(self.base_url, content))
        elif tag.lower() == "a" and values.get("href"):
            self.links.add(urljoin(self.base_url, values["href"]))
        elif tag.lower() == "img" and values.get("src"):
            image_url = urljoin(self.base_url, values["src"])
            self.images.add(image_url)
            marker = " ".join(
                filter(None, [values.get("class"), values.get("id"), values.get("alt")])
            ).lower()
            if any(word in marker for word in ("avatar", "profile", "portrait", "userpic")):
                self.avatar_candidates.append(image_url)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self.in_title:
            self.title_parts.append(cleaned)


@dataclass(frozen=True)
class ArchivedPageSnapshot:
    timestamp: str
    original_url: str
    archive_url: str
    title: str | None
    description: str | None
    emails: tuple[str, ...]
    links: tuple[str, ...]
    images: tuple[str, ...]
    avatar_candidates: tuple[str, ...]
    content_sha256: str
    retrieved_at: datetime
    username: str | None = None
    location: str | None = None
    followers: int | None = None
    following: int | None = None
    linked_sites: tuple[str, ...] = ()
    document_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchivedAvatarComparison:
    first_source_url: str | None
    latest_source_url: str | None
    first_archive_url: str | None
    latest_archive_url: str | None
    first_sha256: str | None
    latest_sha256: str | None
    first_dhash: str | None
    latest_dhash: str | None
    distance: int | None
    classification: str
    error: str | None = None


@dataclass(frozen=True)
class ArchivedPageComparison:
    first: ArchivedPageSnapshot
    latest: ArchivedPageSnapshot
    title_changed: bool
    description_changed: bool
    added_emails: tuple[str, ...]
    removed_emails: tuple[str, ...]
    added_links: tuple[str, ...]
    removed_links: tuple[str, ...]
    added_images: tuple[str, ...]
    removed_images: tuple[str, ...]
    avatar: ArchivedAvatarComparison | None = None


@dataclass(frozen=True)
class IdentityObservation:
    timestamp: str
    captured_at: datetime
    archive_url: str
    display_name: str | None
    biography: str | None
    avatar_source_url: str | None
    avatar_archive_url: str | None
    avatar_sha256: str | None
    avatar_dhash: str | None
    avatar_error: str | None = None
    username: str | None = None
    location: str | None = None
    followers: int | None = None
    following: int | None = None
    linked_sites: tuple[str, ...] = ()
    document_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityChange:
    timestamp: str
    captured_at: datetime
    change_type: str
    previous_value: str | None
    current_value: str | None
    distance: int | None = None


@dataclass(frozen=True)
class IdentityEvolution:
    original_url: str
    identity_label: str | None
    identity_first_seen: datetime
    first_seen: datetime
    last_seen: datetime
    observations: tuple[IdentityObservation, ...]
    changes: tuple[IdentityChange, ...]
    analysed_capture_count: int
    available_capture_count: int


@dataclass(frozen=True)
class SavePageNowResult:
    target_url: str
    submitted_at: datetime
    status_code: int
    archive_url: str | None
    job_id: str | None
    message: str


@dataclass(frozen=True)
class WaybackCapture:
    timestamp: str
    original_url: str
    status_code: int | None
    mime_type: str | None
    digest: str | None
    length: int | None

    @property
    def captured_at(self) -> datetime | None:
        try:
            return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return None

    @property
    def archive_url(self) -> str:
        return f"https://web.archive.org/web/{self.timestamp}/{self.original_url}"


def normalise_target(
    value: str, *, wildcard_domain: bool = False, prefix_target: bool = False
) -> str:
    target = value.strip()
    if not target:
        raise ValueError("Wayback target is required")
    if "://" not in target:
        host = target.lower().rstrip("/")
        if any(character.isspace() for character in host) or "." not in host:
            raise ValueError("Wayback target must be a domain or HTTP(S) URL")
        if prefix_target:
            raise ValueError("Wayback prefix targeting requires an HTTP(S) URL")
        return f"{host}/*" if wildcard_domain else host
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Wayback target must be a domain or HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in Wayback targets")
    if prefix_target:
        if "*" in target:
            raise ValueError("Wayback prefix target must not contain a wildcard")
        return target.rstrip("/") + "*"
    return target


def normalise_save_url(value: str) -> str:
    target = value.strip()
    if not target:
        raise ValueError("public page URL is required")
    if len(target) > 2048 or any(character.isspace() for character in target):
        raise ValueError("invalid public page URL")
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid public page URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Save Page Now requires a public HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in Save Page Now URLs")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private or local URLs cannot be submitted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("Save Page Now requires a public hostname") from None
    else:
        if not address.is_global:
            raise ValueError("private or non-routable URLs cannot be submitted")
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _archive_result_url(value: str | None) -> str | None:
    if not value:
        return None
    candidate = urljoin(SAVE_PAGE_ENDPOINT, value)
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname != "web.archive.org":
        return None
    if not parsed.path.startswith("/web/"):
        return None
    return candidate


async def save_page_now(
    value: str, *, client: httpx.AsyncClient | None = None
) -> SavePageNowResult:
    target = normalise_save_url(value)
    validate_external_egress(
        SAVE_PAGE_ENDPOINT,
        payload_kind=EgressPayloadKind.URL_REFERENCE,
        allowed_hosts=frozenset({"web.archive.org"}),
    )
    owns_client = client is None
    active_client = client or safe_client(
        timeout=httpx.Timeout(30.0), user_agent="OSINT-Workbench/1"
    )
    submitted_at = datetime.now(UTC)
    try:
        async with active_client.stream(
            "POST",
            SAVE_PAGE_ENDPOINT,
            data={"url": target},
            headers={
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.1",
                "User-Agent": "OSINT-Workbench/1",
            },
        ) as response:
            status_code = response.status_code
            archive_url = _archive_result_url(
                response.headers.get("content-location") or response.headers.get("location")
            )
            job_id = response.headers.get("x-archive-save-job-id") or response.headers.get(
                "x-archive-job-id"
            )
            runtime_error = response.headers.get("x-archive-wayback-runtime-error")
    except httpx.HTTPError as exc:
        raise WaybackError("Save Page Now request failed") from exc
    finally:
        if owns_client:
            await active_client.aclose()

    if runtime_error:
        cleaned = " ".join(runtime_error.split())[:240]
        raise WaybackError(f"Save Page Now rejected the capture: {cleaned}")
    if status_code == 429:
        raise WaybackError("Save Page Now rate limit reached")
    if status_code >= 500:
        raise WaybackError("Save Page Now is temporarily unavailable")
    if status_code >= 400:
        raise WaybackError(f"Save Page Now rejected the request with status {status_code}")
    if 300 <= status_code < 400 and archive_url is None:
        raise WaybackError("Save Page Now returned an unexpected redirect")

    return SavePageNowResult(
        target_url=target,
        submitted_at=submitted_at,
        status_code=status_code,
        archive_url=archive_url,
        job_id=job_id,
        message="capture available" if archive_url else "capture submitted",
    )


def build_cdx_url(
    target: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    limit: int = MAX_CAPTURES,
) -> str:
    bounded_limit = max(1, min(MAX_CAPTURES, limit))
    params: list[tuple[str, str]] = [
        ("url", target),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode,mimetype,digest,length"),
        ("filter", "statuscode:200"),
        ("collapse", "digest"),
        ("limit", str(bounded_limit)),
    ]
    if from_year is not None:
        params.append(("from", str(from_year)))
    if to_year is not None:
        params.append(("to", str(to_year)))
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


def parse_cdx_payload(payload: object) -> list[WaybackCapture]:
    if not isinstance(payload, list) or not payload:
        return []
    rows = payload[1:] if isinstance(payload[0], list) else payload
    captures: list[WaybackCapture] = []
    seen: set[tuple[str | None, str]] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        timestamp, original, status_code, mime_type, digest, length = row[:6]
        if not isinstance(timestamp, str) or not isinstance(original, str):
            continue
        key = (digest if isinstance(digest, str) and digest else None, original)
        if key in seen:
            continue
        seen.add(key)
        try:
            parsed_status = int(status_code) if status_code not in {None, ""} else None
        except (TypeError, ValueError):
            parsed_status = None
        try:
            parsed_length = int(length) if length not in {None, ""} else None
        except (TypeError, ValueError):
            parsed_length = None
        captures.append(
            WaybackCapture(
                timestamp=timestamp,
                original_url=original,
                status_code=parsed_status,
                mime_type=mime_type if isinstance(mime_type, str) and mime_type else None,
                digest=digest if isinstance(digest, str) and digest else None,
                length=parsed_length,
            )
        )
    return sorted(captures, key=lambda capture: capture.timestamp)


def _archive_url(timestamp: str, original_url: str) -> str:
    if not re.fullmatch(r"\d{14}", timestamp):
        raise ValueError("invalid Wayback timestamp")
    parsed = urlsplit(original_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid archived page URL")
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


def _archived_username(original_url: str, meta: dict[str, str]) -> str | None:
    parsed = urlsplit(original_url)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"instagram.com", "x.com", "twitter.com", "github.com"} and parts:
        return parts[0].removeprefix("@").casefold() or None
    if host == "tiktok.com" and parts:
        return parts[0].removeprefix("@").casefold() or None
    if host.endswith("reddit.com") and len(parts) >= 2 and parts[0].casefold() in {"u", "user"}:
        return parts[1].casefold() or None
    for key in ("profile:username", "twitter:creator"):
        candidate = meta.get(key, "").strip().removeprefix("@").strip()
        if candidate and re.fullmatch(r"[\w.\-]{2,64}", candidate, flags=re.UNICODE):
            return candidate.casefold()
    return None


def _archived_counter(text: str, label: str) -> int | None:
    match = re.search(rf"(?i)(\d{{1,3}}(?:[ ,.]\d{{3}})*)\s+{label}\b", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if 0 <= value <= 10**12 else None


def _archived_location(meta: dict[str, str]) -> str | None:
    for key in ("profile:location", "og:locality", "place:location", "geo.placename"):
        candidate = " ".join(meta.get(key, "").split())
        if 1 <= len(candidate) <= 200:
            return candidate
    return None


def _archived_link_metadata(
    original_url: str, links: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    source_host = (urlsplit(original_url).hostname or "").casefold().removeprefix("www.")
    linked_sites: list[str] = []
    documents: list[str] = []
    seen_sites: set[str] = set()
    seen_documents: set[str] = set()
    document_suffixes = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
    for link in sorted(links)[:250]:
        parsed = urlsplit(link)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or host == "web.archive.org":
            continue
        path = parsed.path.casefold()
        if path.endswith(document_suffixes) and link not in seen_documents:
            seen_documents.add(link)
            documents.append(link)
        if host != source_host and host not in seen_sites:
            seen_sites.add(host)
            linked_sites.append(link)
        if len(linked_sites) >= 20 and len(documents) >= 20:
            break
    return tuple(linked_sites[:20]), tuple(documents[:20])


def parse_archived_page(
    content: bytes, *, timestamp: str, original_url: str, archive_url: str
) -> ArchivedPageSnapshot:
    parser = _ArchivedPageParser(original_url)
    text = content.decode("utf-8", errors="replace")
    parser.feed(text)
    visible_text = " ".join(parser.text_parts)
    emails = tuple(sorted(set(EMAIL_PATTERN.findall(visible_text))))
    linked_sites, document_links = _archived_link_metadata(original_url, parser.links)
    return ArchivedPageSnapshot(
        timestamp=timestamp,
        original_url=original_url,
        archive_url=archive_url,
        title=" ".join(parser.title_parts) or None,
        description=parser.description,
        emails=emails,
        links=tuple(sorted(parser.links)),
        images=tuple(sorted(parser.images)),
        avatar_candidates=tuple(dict.fromkeys(parser.avatar_candidates)),
        content_sha256=hashlib.sha256(content).hexdigest(),
        retrieved_at=datetime.now(UTC),
        username=_archived_username(original_url, parser.meta),
        location=_archived_location(parser.meta),
        followers=_archived_counter(visible_text, "followers"),
        following=_archived_counter(visible_text, "following"),
        linked_sites=linked_sites,
        document_links=document_links,
    )


async def fetch_archived_page(timestamp: str, original_url: str) -> ArchivedPageSnapshot:
    archive_url = _archive_url(timestamp, original_url)
    try:
        async with safe_client(timeout=20.0, user_agent="OSINT-Workbench/0.1") as client:
            async with client.stream("GET", archive_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    raise WaybackError("archived capture is not HTML")
                try:
                    content = await read_bounded_response(
                        response, max_bytes=MAX_ARCHIVED_PAGE_BYTES
                    )
                except EgressPolicyError as exc:
                    raise WaybackError("archived page exceeds the 1 MiB limit") from exc
        return parse_archived_page(
            content,
            timestamp=timestamp,
            original_url=original_url,
            archive_url=archive_url,
        )
    except httpx.HTTPError as exc:
        raise WaybackError("archived page retrieval failed") from exc


def archived_asset_url(timestamp: str, source_url: str) -> str:
    return _archive_url(timestamp, source_url)


def select_timeline_captures(
    captures: list[WaybackCapture], *, limit: int = 25
) -> list[WaybackCapture]:
    ordered = sorted(captures, key=lambda capture: capture.timestamp)
    if len(ordered) <= limit:
        return ordered
    if limit < 2:
        return ordered[:1]
    selected_indexes = {round(index * (len(ordered) - 1) / (limit - 1)) for index in range(limit)}
    return [ordered[index] for index in sorted(selected_indexes)]


async def _identity_observation(
    capture: WaybackCapture, cache: ImageCache | None
) -> IdentityObservation:
    snapshot = await fetch_archived_page(capture.timestamp, capture.original_url)
    captured_at = capture.captured_at
    if captured_at is None:
        raise WaybackError("Wayback capture has an invalid timestamp")
    avatar_source = snapshot.avatar_candidates[0] if snapshot.avatar_candidates else None
    avatar_archive = (
        archived_asset_url(snapshot.timestamp, avatar_source) if avatar_source else None
    )
    avatar_sha256: str | None = None
    avatar_dhash: str | None = None
    avatar_error: str | None = None
    if avatar_archive and cache:
        try:
            image = await cache.fetch(avatar_archive)
            avatar_sha256 = image.sha256
            avatar_dhash = perceptual_dhash(image.path)
        except (ImageCacheError, ValueError) as exc:
            avatar_error = str(exc)
    return IdentityObservation(
        timestamp=snapshot.timestamp,
        captured_at=captured_at,
        archive_url=snapshot.archive_url,
        display_name=snapshot.title,
        biography=snapshot.description,
        avatar_source_url=avatar_source,
        avatar_archive_url=avatar_archive,
        avatar_sha256=avatar_sha256,
        avatar_dhash=avatar_dhash,
        avatar_error=avatar_error,
        username=snapshot.username,
        location=snapshot.location,
        followers=snapshot.followers,
        following=snapshot.following,
        linked_sites=snapshot.linked_sites,
        document_links=snapshot.document_links,
    )


def _identity_changes(observations: list[IdentityObservation]) -> list[IdentityChange]:
    changes: list[IdentityChange] = []
    for previous, current in zip(observations, observations[1:], strict=False):
        if previous.display_name != current.display_name:
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "display_name_changed",
                    previous.display_name,
                    current.display_name,
                )
            )
        if previous.biography != current.biography:
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "biography_changed",
                    previous.biography,
                    current.biography,
                )
            )
        for change_type, previous_value, current_value in (
            ("username_changed", previous.username, current.username),
            ("location_changed", previous.location, current.location),
            (
                "followers_changed",
                str(previous.followers) if previous.followers is not None else None,
                str(current.followers) if current.followers is not None else None,
            ),
            (
                "following_changed",
                str(previous.following) if previous.following is not None else None,
                str(current.following) if current.following is not None else None,
            ),
        ):
            if previous_value != current_value:
                changes.append(
                    IdentityChange(
                        current.timestamp,
                        current.captured_at,
                        change_type,
                        previous_value,
                        current_value,
                    )
                )
        for link in sorted(set(current.linked_sites) - set(previous.linked_sites)):
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "linked_site_added",
                    None,
                    link,
                )
            )
        for link in sorted(set(previous.linked_sites) - set(current.linked_sites)):
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "linked_site_removed",
                    link,
                    None,
                )
            )
        for link in sorted(set(current.document_links) - set(previous.document_links)):
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "document_link_added",
                    None,
                    link,
                )
            )
        for link in sorted(set(previous.document_links) - set(current.document_links)):
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "document_link_removed",
                    link,
                    None,
                )
            )
        if not previous.avatar_source_url and current.avatar_source_url:
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "profile_picture_added",
                    None,
                    current.avatar_sha256 or current.avatar_source_url,
                )
            )
        elif previous.avatar_source_url and not current.avatar_source_url:
            changes.append(
                IdentityChange(
                    current.timestamp,
                    current.captured_at,
                    "profile_picture_removed",
                    previous.avatar_sha256 or previous.avatar_source_url,
                    None,
                )
            )
        elif previous.avatar_source_url and current.avatar_source_url:
            distance = None
            materially_changed = previous.avatar_source_url != current.avatar_source_url
            if previous.avatar_sha256 and current.avatar_sha256:
                materially_changed = previous.avatar_sha256 != current.avatar_sha256
            if previous.avatar_dhash and current.avatar_dhash:
                distance = hamming_distance(previous.avatar_dhash, current.avatar_dhash)
                materially_changed = distance > 8
            if materially_changed:
                changes.append(
                    IdentityChange(
                        current.timestamp,
                        current.captured_at,
                        "profile_picture_changed",
                        previous.avatar_sha256 or previous.avatar_source_url,
                        current.avatar_sha256 or current.avatar_source_url,
                        distance,
                    )
                )
    return changes


async def build_identity_evolution(
    captures: list[WaybackCapture],
    *,
    image_cache: ImageCache | None = None,
    limit: int = 25,
) -> IdentityEvolution:
    if not captures:
        raise ValueError("at least one Wayback capture is required")
    original_urls = {capture.original_url for capture in captures}
    if len(original_urls) != 1:
        raise ValueError("identity evolution requires captures for one original URL")
    selected = select_timeline_captures(captures, limit=max(1, min(25, limit)))
    observations: list[IdentityObservation] = []
    for capture in selected:
        observations.append(await _identity_observation(capture, image_cache))
    identity_label = next(
        (
            observation.display_name
            for observation in reversed(observations)
            if observation.display_name
        ),
        None,
    )
    identity_first_seen = next(
        (
            observation.captured_at
            for observation in observations
            if identity_label and observation.display_name == identity_label
        ),
        observations[0].captured_at,
    )
    return IdentityEvolution(
        original_url=selected[0].original_url,
        identity_label=identity_label,
        identity_first_seen=identity_first_seen,
        first_seen=observations[0].captured_at,
        last_seen=observations[-1].captured_at,
        observations=tuple(observations),
        changes=tuple(_identity_changes(observations)),
        analysed_capture_count=len(observations),
        available_capture_count=len(captures),
    )


async def compare_archived_avatars(
    first: ArchivedPageSnapshot,
    latest: ArchivedPageSnapshot,
    cache: ImageCache,
) -> ArchivedAvatarComparison:
    first_source = first.avatar_candidates[0] if first.avatar_candidates else None
    latest_source = latest.avatar_candidates[0] if latest.avatar_candidates else None
    if not first_source and not latest_source:
        return ArchivedAvatarComparison(
            None, None, None, None, None, None, None, None, None, "unavailable"
        )
    if not first_source or not latest_source:
        return ArchivedAvatarComparison(
            first_source,
            latest_source,
            archived_asset_url(first.timestamp, first_source) if first_source else None,
            archived_asset_url(latest.timestamp, latest_source) if latest_source else None,
            None,
            None,
            None,
            None,
            None,
            "added" if latest_source else "removed",
        )
    first_archive = archived_asset_url(first.timestamp, first_source)
    latest_archive = archived_asset_url(latest.timestamp, latest_source)
    try:
        first_image = await cache.fetch(first_archive)
        latest_image = await cache.fetch(latest_archive)
        first_dhash = perceptual_dhash(first_image.path)
        latest_dhash = perceptual_dhash(latest_image.path)
        distance = hamming_distance(first_dhash, latest_dhash)
        classification = (
            "exact"
            if first_image.sha256 == latest_image.sha256
            else "similar"
            if distance <= 8
            else "changed"
        )
        return ArchivedAvatarComparison(
            first_source,
            latest_source,
            first_archive,
            latest_archive,
            first_image.sha256,
            latest_image.sha256,
            first_dhash,
            latest_dhash,
            distance,
            classification,
        )
    except (ImageCacheError, ValueError) as exc:
        return ArchivedAvatarComparison(
            first_source,
            latest_source,
            first_archive,
            latest_archive,
            None,
            None,
            None,
            None,
            None,
            "unavailable",
            str(exc),
        )


async def compare_archived_pages(
    original_url: str,
    first_timestamp: str,
    latest_timestamp: str,
    *,
    image_cache: ImageCache | None = None,
) -> ArchivedPageComparison:
    first = await fetch_archived_page(first_timestamp, original_url)
    latest = await fetch_archived_page(latest_timestamp, original_url)
    avatar = await compare_archived_avatars(first, latest, image_cache) if image_cache else None
    return ArchivedPageComparison(
        first=first,
        latest=latest,
        title_changed=first.title != latest.title,
        description_changed=first.description != latest.description,
        added_emails=tuple(sorted(set(latest.emails) - set(first.emails))),
        removed_emails=tuple(sorted(set(first.emails) - set(latest.emails))),
        added_links=tuple(sorted(set(latest.links) - set(first.links))),
        removed_links=tuple(sorted(set(first.links) - set(latest.links))),
        added_images=tuple(sorted(set(latest.images) - set(first.images))),
        removed_images=tuple(sorted(set(first.images) - set(latest.images))),
        avatar=avatar,
    )


async def discover_captures(
    target: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    limit: int = MAX_CAPTURES,
) -> list[WaybackCapture]:
    url = build_cdx_url(target, from_year=from_year, to_year=to_year, limit=limit)
    try:
        async with safe_client(timeout=20.0, user_agent="OSINT-Workbench/0.1") as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content = await read_bounded_response(
                    response, max_bytes=MAX_CDX_RESPONSE_BYTES
                )
            return parse_cdx_payload(json.loads(content.decode("utf-8")))
    except (httpx.HTTPError, EgressPolicyError, UnicodeDecodeError, ValueError) as exc:
        raise WaybackError("Wayback CDX lookup failed") from exc

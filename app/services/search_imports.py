import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db import AuditEvent, Finding, Investigation, ToolRun
from app.services.location_intelligence import validate_source_url
from app.services.profile_links import profile_account_from_url

MAX_IMPORT_BYTES = 64 * 1024
MAX_IMPORT_RESULTS = 50
URL_LINE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


@dataclass(frozen=True)
class SearchResultItem:
    url: str
    title: str | None
    snippet: str | None


@dataclass(frozen=True)
class SearchImportSummary:
    submitted_count: int
    imported_count: int
    duplicate_count: int
    rejected_count: int
    run_id: str


def unwrap_search_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    host = (parsed.hostname or "").casefold()
    if (
        host in {"google.com", "www.google.com", "google.ie", "www.google.ie"}
        and parsed.path == "/url"
    ):
        params = parse_qs(parsed.query)
        values = params.get("q") or params.get("url")
        target = values[0] if values else None
        if isinstance(target, str):
            return target
    if host in {"bing.com", "www.bing.com"} and parsed.path == "/ck/a":
        values = parse_qs(parsed.query).get("u")
        encoded = values[0] if values else None
        if isinstance(encoded, str) and encoded.startswith("a1"):
            payload = encoded[2:]
            payload += "=" * (-len(payload) % 4)
            try:
                decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
            else:
                if decoded.startswith(("http://", "https://")):
                    return decoded
    return value


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    clean = re.sub(r"\s+", " ", str(value)).strip()
    return clean[:limit] if clean else None


def _json_items(payload: object) -> list[tuple[object, object, object]]:
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("items", [payload]))
    if not isinstance(payload, list):
        raise ValueError("JSON import must contain a result object or result list")
    items: list[tuple[object, object, object]] = []
    for entry in payload:
        if isinstance(entry, str):
            items.append((entry, None, None))
        elif isinstance(entry, dict):
            items.append(
                (
                    entry.get("url") or entry.get("link") or entry.get("href"),
                    entry.get("title") or entry.get("name"),
                    entry.get("snippet") or entry.get("description") or entry.get("text"),
                )
            )
        else:
            items.append((None, None, None))
    return items


def _line_items(raw: str) -> list[tuple[object, object, object]]:
    items: list[tuple[object, object, object]] = []
    for line in raw.splitlines():
        clean = line.strip()
        if not clean:
            continue
        fields = [field.strip() for field in clean.split("\t", 2)]
        if len(fields) == 1 and not URL_LINE_RE.fullmatch(fields[0]):
            pipe_fields = [field.strip() for field in clean.split(" | ", 2)]
            fields = pipe_fields if URL_LINE_RE.fullmatch(pipe_fields[0]) else fields
        items.append(
            (
                fields[0] if fields else None,
                fields[1] if len(fields) > 1 else None,
                fields[2] if len(fields) > 2 else None,
            )
        )
    return items


def parse_search_results(raw: str) -> tuple[list[SearchResultItem], int]:
    encoded_size = len(raw.encode("utf-8"))
    if not raw.strip():
        raise ValueError("paste at least one search result")
    if encoded_size > MAX_IMPORT_BYTES:
        raise ValueError("search-result import exceeds the 64 KiB limit")
    stripped = raw.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            entries = _json_items(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("search-result JSON is invalid") from exc
    else:
        entries = _line_items(raw)
    if len(entries) > MAX_IMPORT_RESULTS:
        raise ValueError(f"a maximum of {MAX_IMPORT_RESULTS} results can be imported at once")

    rejected = 0
    unique: dict[str, SearchResultItem] = {}
    for url_value, title_value, snippet_value in entries:
        if not isinstance(url_value, str):
            rejected += 1
            continue
        try:
            canonical = validate_source_url(unwrap_search_url(url_value))
        except ValueError:
            rejected += 1
            continue
        if canonical is None:
            rejected += 1
            continue
        unique.setdefault(
            canonical,
            SearchResultItem(
                url=canonical,
                title=_text(title_value, 500),
                snippet=_text(snippet_value, 2000),
            ),
        )
    if not unique:
        raise ValueError("no valid public HTTP(S) result URLs were supplied")
    return list(unique.values()), rejected


_SNIPPET_COUNT_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?\s?[KkMmBb]?)\s+"
    r"(followers|following|posts|likes|subscribers|videos)\b",
    re.IGNORECASE,
)
_TITLE_NAME_RE = re.compile(r"^\s*(.{1,80}?)\s*\(@[\w.\-]{1,64}\)")


def snippet_profile_details(item: SearchResultItem) -> list[dict[str, str]]:
    """Read the counts and display name a search engine shows for an account page.

    These are the engine's cached view of the page, so they can be out of date; every label
    says where the value came from.
    """
    details: list[dict[str, str]] = []
    name = _TITLE_NAME_RE.match(item.title or "")
    if name:
        details.append({"label": "Display name (search result title)", "value": name[1].strip()})
    seen: set[str] = set()
    for value, label in _SNIPPET_COUNT_RE.findall(item.snippet or ""):
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        details.append({"label": f"{label.capitalize()} (search snippet)", "value": value.strip()})
    return details[:8]


def import_search_result_items(
    db: Session,
    investigation: Investigation,
    items: list[SearchResultItem],
    rejected: int,
    *,
    engine: str,
    query: str,
    query_id: str,
    query_label: str,
    source_tool: str = "search_import",
    tool_display_name: str = "Search result import",
    event_type: str = "search_results_imported",
) -> SearchImportSummary:
    now = datetime.now(UTC)
    run = ToolRun(
        investigation_id=investigation.id,
        tool_id=source_tool,
        display_name=tool_display_name,
        status="completed",
        timeout_seconds=0,
        query_value=query[:320],
        query_type=investigation.query_type,
        started_at=now,
        finished_at=now,
        duration_ms=0,
    )
    db.add(run)
    db.flush()

    existing = {
        finding.canonical_url: finding
        for finding in db.scalars(
            select(Finding).where(
                Finding.investigation_id == investigation.id,
                Finding.canonical_url.is_not(None),
            )
        )
        if finding.canonical_url
    }
    imported = 0
    duplicates = 0
    for item in items:
        provenance = {
            "tool": source_tool,
            "engine": engine,
            "query_id": query_id,
            "query": query,
            "title": item.title,
            "snippet": item.snippet,
            "imported_at": now.isoformat(),
        }
        # A result that is itself an account page names a handle worth searching next.
        account = profile_account_from_url(item.url)
        linked_account = (
            {"platform": account[0], "username": account[1], "source": "search_result_url"}
            if account
            else None
        )
        snippet_details = snippet_profile_details(item) if account else []
        stored = existing.get(item.url)
        if stored is not None:
            duplicates += 1
            source_tools = list(stored.source_tools)
            if source_tool not in source_tools:
                source_tools.append(source_tool)
            metadata = dict(stored.extra)
            metadata["provenance"] = [*metadata.get("provenance", []), provenance][-100:]
            metadata[source_tool] = {
                "last_engine": engine,
                "last_query_id": query_id,
                "last_imported_at": now.isoformat(),
            }
            if stored.status not in {"found", "likely"}:
                observations = list(metadata.get("status_observations", []))
                if not observations:
                    observations.append(
                        {
                            "source": stored.source_tool,
                            "status": stored.status,
                            "observed_at": stored.observed_at.isoformat(),
                        }
                    )
                observations.append(
                    {
                        "source": source_tool,
                        "status": "likely",
                        "observed_at": now.isoformat(),
                    }
                )
                metadata["status_observations"] = observations[-100:]
                stored.status = "likely"
            if linked_account and "linked_account" not in metadata:
                metadata["linked_account"] = linked_account
            if snippet_details and not metadata.get("platform_details"):
                metadata["platform_details"] = snippet_details
            stored.source_tools = source_tools
            stored.extra = metadata
            if not stored.title and item.title:
                stored.title = item.title
            if not stored.description and item.snippet:
                stored.description = item.snippet
            continue

        digest = hashlib.sha256(item.url.encode()).hexdigest()[:24]
        platform = (urlsplit(item.url).hostname or "").removeprefix("www.") or None
        finding = Finding(
            investigation_id=investigation.id,
            tool_run_id=run.id,
            query_type=investigation.query_type,
            queried_value=investigation.query,
            finding_type="search_result",
            platform=platform,
            value=item.title or item.url,
            canonical_url=item.url,
            display_url=item.url,
            status="likely",
            confidence=None,
            title=item.title,
            description=item.snippet,
            extra={
                source_tool: {
                    "engine": engine,
                    "query_id": query_id,
                    "query_label": query_label,
                    "query": query,
                    "imported_at": now.isoformat(),
                },
                "provenance": [provenance],
                "profile": {},
                **({"linked_account": linked_account} if linked_account else {}),
                **({"platform_details": snippet_details} if snippet_details else {}),
            },
            source_tool=source_tool,
            source_tools=[source_tool],
            source_record_id=digest,
            observed_at=now,
        )
        db.add(finding)
        existing[item.url] = finding
        imported += 1

    db.add(
        AuditEvent(
            investigation_id=investigation.id,
            event_type=event_type,
            details={
                "engine": engine,
                "query_id": query_id,
                "query_label": query_label,
                "query": query,
                "submitted_count": len(items),
                "imported_count": imported,
                "duplicate_count": duplicates,
                "rejected_count": rejected,
                "run_id": run.id,
            },
        )
    )
    return SearchImportSummary(
        submitted_count=len(items),
        imported_count=imported,
        duplicate_count=duplicates,
        rejected_count=rejected,
        run_id=run.id,
    )


def import_search_results(
    db: Session,
    investigation: Investigation,
    *,
    raw: str,
    engine: str,
    query: str,
    query_id: str,
    query_label: str,
) -> SearchImportSummary:
    items, rejected = parse_search_results(raw)
    return import_search_result_items(
        db,
        investigation,
        items,
        rejected,
        engine=engine,
        query=query,
        query_id=query_id,
        query_label=query_label,
    )

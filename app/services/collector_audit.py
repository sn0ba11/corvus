"""Build a human-reviewable presentation from retained collector-audit output."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.adapters import get_adapter
from app.models.schemas import QueryType

GAP_BASE_FIELDS = frozenset(
    {
        "site",
        "platform",
        "url",
        "profile_url",
        "status",
        "found",
        "exists",
        "value",
        "username",
        "tags",
        "http_status",
        "rank",
        "_extractor",
        "platform_details",
        "timeline_events",
        "public_enrichment",
        "evidence_url",
        "category",
        "metadata",
        "candidate_generator",
        "social_analyzer_rate",
        "social_analyzer_title",
        "social_analyzer_text",
        "source_metadata",
        "authenticated_session",
        "maigret_upstream_status",
        "maigret_unverified_claim",
    }
)
GAP_IGNORED_PREFIXES = ("sherlock_", "blackbird_", "verification_", "_", "http_")
GAP_KNOWN_PRESENTED_FIELDS = frozenset(
    {
        "avatar",
        "avatar_url",
        "image",
        "image_bg",
        "picture",
        "photo",
        "profile_image_url",
        "profile_pic_url",
        "imgur_profile_avatar_url",
        "gravatar_url",
        "snapcode_image",
        "is_private",
        "is_protected",
        "is_pro",
        "is_premium",
        "is_verified_merchant",
        "is_website_verified",
        "isProfileStatusActive",
        "fromLanguage",
        "learningLanguage",
        "all_views_count",
    }
)
GAP_HIGH_TERMS = (
    "email",
    "phone",
    "username",
    "fullname",
    "display_name",
    "user_id",
    "uid",
    "avatar",
    "image",
    "picture",
    "location",
    "city",
    "country",
    "created",
    "joined",
    "last_seen",
    "last_activity",
    "birthday",
    "birth_date",
    "gender",
    "wallet",
)
GAP_MEDIUM_TERMS = (
    "count",
    "views",
    "likes",
    "posts",
    "videos",
    "reputation",
    "status",
    "role",
    "verified",
    "private",
    "premium",
    "is_pro",
    "ranking",
    "currency",
    "website",
    "language",
    "timezone",
    "tagline",
)

MAX_AUDIT_JSON_BYTES = 5 * 1024 * 1024

STATUS_ORDER = {
    "found": 5,
    "likely": 4,
    "unknown": 3,
    "not_found": 2,
    "rate_limited": 1,
    "error": 0,
}


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        if path.stat().st_size > MAX_AUDIT_JSON_BYTES:
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    candidate = str(value).strip()
    return candidate or None


def _http_link(value: Any) -> str | None:
    candidate = _text(value)
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _profile(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("profile")
    return dict(value) if isinstance(value, dict) else {}


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _record_sources(record: dict[str, Any]) -> list[str]:
    values = record.get("source_tools")
    if isinstance(values, list):
        sources = [_text(value) for value in values]
        retained = [value for value in sources if value]
        if retained:
            return list(dict.fromkeys(retained))
    return [_text(record.get("source_tool")) or "unknown"]


def _detail_items(record: dict[str, Any]) -> list[dict[str, str]]:
    details = _metadata(record).get("platform_details")
    if not isinstance(details, list):
        return []
    retained: list[dict[str, str]] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        label, value = _text(item.get("label")), _text(item.get("value"))
        if label and value:
            retained.append({"label": label, "value": value})
    return retained


def _timeline_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    events = _metadata(record).get("timeline_events")
    if not isinstance(events, list):
        return []
    retained: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = _text(event.get("timestamp"))
        event_type = _text(event.get("type"))
        if not timestamp or not event_type:
            continue
        retained_event: dict[str, Any] = {
            "type": event_type,
            "label": _text(event.get("label")) or event_type.replace("_", " ").title(),
            "timestamp": timestamp,
            "source_field": _text(event.get("source_field")) or "unknown",
            "raw_value": _text(event.get("raw_value")) or timestamp,
            "sources": _record_sources(record),
        }
        for key in (
            "semantic",
            "category",
            "precision",
            "confidence",
            "source_kind",
            "extractor",
            "note",
        ):
            value = _text(event.get(key))
            if value:
                retained_event[key] = value
        retained_event["include_in_timeline"] = event.get("include_in_timeline") is not False
        retained.append(retained_event)
    return retained


def _gap_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    if value in (None, "", [], {}):
        return tokens
    if isinstance(value, bool):
        return {"true", "yes", "1"} if value else {"false", "no", "0"}
    if isinstance(value, int | float):
        return {str(value).replace(",", "").casefold()}
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip().casefold()
        if text in {"true", "yes", "1"}:
            return {"true", "yes", "1"}
        if text in {"false", "no", "0"}:
            return {"false", "no", "0"}
        if text:
            tokens.add(text)
            tokens.add(text.replace(",", ""))
        return tokens
    if isinstance(value, list):
        if len(value) > 30:
            return tokens
        for item in value:
            if isinstance(item, str | int | float | bool):
                tokens.update(_gap_tokens(item))
        tokens.update(_gap_tokens(str(value)))
        return tokens
    if isinstance(value, dict):
        if len(value) > 20:
            return tokens
        for item in value.values():
            if isinstance(item, str | int | float | bool):
                tokens.update(_gap_tokens(item))
        return tokens
    return tokens


def _gap_priority(field: str) -> str | None:
    key = field.casefold()
    if any(term in key for term in GAP_HIGH_TERMS):
        return "high"
    if any(term in key for term in GAP_MEDIUM_TERMS):
        return "medium"
    return None


def analyse_parsing_gaps(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify likely identity-bearing metadata not yet represented in cards or timeline."""
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        metadata = _metadata(record)
        presented_tokens: set[str] = set()
        for value in _profile(record).values():
            presented_tokens.update(_gap_tokens(value))
        for detail in _detail_items(record):
            presented_tokens.update(_gap_tokens(detail["value"]))
        for event in _timeline_items(record):
            presented_tokens.update(_gap_tokens(event.get("raw_value")))
        presented_tokens.update(_gap_tokens(record.get("canonical_url")))
        presented_tokens.update(_gap_tokens(record.get("display_url")))

        fields_to_check = list(metadata.items())
        # Blackbird's labelled values otherwise disappear behind the ignored
        # metadata container, hiding omissions in our field allowlist.
        labelled = metadata.get("metadata")
        if isinstance(labelled, list):
            for index, entry in enumerate(labelled[:200]):
                if not isinstance(entry, dict):
                    continue
                label = entry.get("name") or entry.get("type")
                if isinstance(label, str) and label.strip():
                    fields_to_check.append((f"metadata[{index}].{label.strip()}", entry.get("value")))

        for field, value in fields_to_check:
            if (
                field in GAP_BASE_FIELDS
                or field in GAP_KNOWN_PRESENTED_FIELDS
                or field.startswith(GAP_IGNORED_PREFIXES)
            ):
                continue
            priority = _gap_priority(field)
            if priority is None:
                continue
            value_tokens = _gap_tokens(value)
            if not value_tokens or value_tokens & presented_tokens:
                continue
            text = _text(value)
            if text is None:
                try:
                    text = json.dumps(value, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    continue
            if len(text) > 1000:
                text = f"{text[:997]}…"
            item = grouped.setdefault(
                field,
                {
                    "field": field,
                    "priority": priority,
                    "occurrences": 0,
                    "platforms": set(),
                    "tools": set(),
                    "examples": [],
                },
            )
            if priority == "high":
                item["priority"] = "high"
            item["occurrences"] += 1
            item["platforms"].add(_text(record.get("platform")) or "Uncategorised")
            item["tools"].add(_text(record.get("source_tool")) or "unknown")
            example = {
                "platform": _text(record.get("platform")) or "Uncategorised",
                "tool": _text(record.get("source_tool")) or "unknown",
                "value": text,
                "url": _http_link(record.get("canonical_url"))
                or _http_link(record.get("display_url")),
            }
            if example not in item["examples"] and len(item["examples"]) < 5:
                item["examples"].append(example)

    fields: list[dict[str, Any]] = []
    for item in grouped.values():
        item["platforms"] = sorted(item["platforms"], key=str.casefold)
        item["tools"] = sorted(item["tools"])
        fields.append(item)
    fields.sort(
        key=lambda item: (
            0 if item["priority"] == "high" else 1,
            -item["occurrences"],
            item["field"].casefold(),
        )
    )
    return {
        "field_count": len(fields),
        "high_priority_count": sum(item["priority"] == "high" for item in fields),
        "medium_priority_count": sum(item["priority"] == "medium" for item in fields),
        "fields": fields,
    }


def _account_key(record: dict[str, Any]) -> str:
    platform = (_text(record.get("platform")) or "unknown").casefold()
    profile = _profile(record)
    identifier = (
        _text(profile.get("account_id"))
        or _text(record.get("queried_value"))
        or _text(record.get("value"))
    )
    if identifier and not identifier.startswith(("http://", "https://")):
        return f"profile::{platform}::{identifier.casefold()}"
    url = _text(record.get("canonical_url")) or _text(record.get("display_url"))
    if url:
        parsed = urlsplit(url)
        return f"url::{parsed.netloc.casefold()}::{parsed.path.rstrip('/').casefold()}"
    source = _text(record.get("source_tool")) or "unknown"
    source_id = _text(record.get("source_record_id"))
    if source_id is None:
        payload = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        source_id = hashlib.sha256(payload).hexdigest()
    return f"source::{source}::{source_id}"


def _account_group_keys(records: list[dict[str, Any]]) -> list[str]:
    """One grouping key per record, keeping every projection of an account together.

    A stored finding is one account, however its sources name it: one collector may report
    the platform's numeric ID and another only the handle, which gives the two records
    different keys (`profile::github::12345` and `profile::github::example`) and used to
    produce two cards for one finding. Records that share a stored finding are therefore
    grouped, and for a username search the handle key of that finding is treated as the same
    account for other findings too. Other search types are not widened this way: two
    different people found by one name search must not be joined through the search term.
    """
    own_keys = [_account_key(record) for record in records]
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Keep the earlier-seen key as the group's name so ordering stays stable.
            parent[right_root] = left_root

    first_key_for_finding: dict[str, str] = {}
    resolved: list[str] = []
    for record, own_key in zip(records, own_keys, strict=True):
        finding_id = _text(record.get("finding_id"))
        if finding_id is None:
            resolved.append(own_key)
            continue
        if _text(record.get("query_type")) == "username":
            group_key = first_key_for_finding.setdefault(finding_id, own_key)
            union(group_key, own_key)
            resolved.append(own_key)
        else:
            # Join this finding's own projections under its first key, without making its
            # other keys stand for the same account elsewhere.
            resolved.append(first_key_for_finding.setdefault(finding_id, own_key))
    return [find(key) if key in parent else key for key in resolved]


def _richness(record: dict[str, Any]) -> int:
    profile = _profile(record)
    profile_points = sum(
        1
        for key in (
            "avatar_url",
            "display_name",
            "biography",
            "location",
            "account_id",
            "verified",
            "followers",
            "following",
        )
        if profile.get(key) not in (None, "")
    )
    return (
        profile_points * 4
        + len(_detail_items(record)) * 2
        + len(_timeline_items(record)) * 3
        + STATUS_ORDER.get(str(record.get("status")), 0)
        + (2 if record.get("canonical_url") else 0)
    )


def _choose_url(existing: str | None, incoming: str | None) -> str | None:
    if not existing:
        return incoming
    if not incoming:
        return existing

    def score(value: str) -> int:
        parsed = urlsplit(value)
        path = parsed.path.casefold()
        points = 0
        if not parsed.query:
            points += 3
        if "/profile/" in path or "/user/" in path or path.startswith("/@"):
            points += 2
        if "/api/" in path or "api." in parsed.netloc.casefold():
            points -= 3
        if parsed.query:
            points -= 2
        return points

    return incoming if score(incoming) > score(existing) else existing


def _merge_profile(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value in (None, ""):
            continue
        current = merged.get(key)
        if key in {"followers", "following"}:
            if current is None or (
                isinstance(current, int) and isinstance(value, int) and value > current
            ):
                merged[key] = value
        elif key == "verified":
            merged[key] = bool(current) or bool(value)
        elif current in (None, ""):
            merged[key] = value
    return merged


def _merge_details(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for item in (*existing, *incoming):
        key = item["label"].casefold()
        values.setdefault(key, item)
        if len(item["value"]) > len(values[key]["value"]):
            values[key] = item
    return sorted(values.values(), key=lambda item: item["label"].casefold())


def _merge_timeline(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for event in (*existing, *incoming):
        key = (str(event["type"]), str(event["timestamp"]))
        if key not in events:
            events[key] = dict(event)
            events[key]["sources"] = list(event.get("sources", []))
            continue
        sources = events[key].setdefault("sources", [])
        for source in event.get("sources", []):
            if source not in sources:
                sources.append(source)
    for event in events.values():
        event["sources"] = sorted(event.get("sources", []))
    return sorted(events.values(), key=lambda item: item["timestamp"], reverse=True)


def _normalise_account(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": _account_key(record),
        "platform": _text(record.get("platform")) or "Uncategorised",
        "status": _text(record.get("status")) or "unknown",
        "confidence": record.get("confidence"),
        "value": _text(record.get("value")) or _text(record.get("queried_value")),
        "url": _http_link(record.get("canonical_url")) or _http_link(record.get("display_url")),
        "profile": _profile(record),
        "avatar_link": _http_link(_profile(record).get("avatar_url")),
        "details": _detail_items(record),
        "timeline_events": _timeline_items(record),
        "sources": _record_sources(record),
        "source_records": [record],
    }


def merge_accounts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate collector records while retaining every source record."""
    merged: dict[str, dict[str, Any]] = {}
    group_keys = _account_group_keys(records)
    for record, key in zip(records, group_keys, strict=True):
        incoming = _normalise_account(record)
        if key not in merged:
            incoming["key"] = key
            merged[key] = incoming
            continue
        account = merged[key]
        if STATUS_ORDER.get(incoming["status"], 0) > STATUS_ORDER.get(account["status"], 0):
            account["status"] = incoming["status"]
        confidences = [
            value
            for value in (account.get("confidence"), incoming.get("confidence"))
            if isinstance(value, int | float)
        ]
        account["confidence"] = max(confidences) if confidences else None
        account["url"] = _choose_url(account.get("url"), incoming.get("url"))
        account["profile"] = _merge_profile(account["profile"], incoming["profile"])
        account["avatar_link"] = _http_link(account["profile"].get("avatar_url"))
        account["details"] = _merge_details(account["details"], incoming["details"])
        account["timeline_events"] = _merge_timeline(
            account["timeline_events"], incoming["timeline_events"]
        )
        for source in incoming["sources"]:
            if source not in account["sources"]:
                account["sources"].append(source)
        account["source_records"].extend(incoming["source_records"])
        if _richness(record) > max(_richness(item) for item in account["source_records"][:-1]):
            account["platform"] = incoming["platform"]
            account["value"] = incoming["value"]

    accounts = list(merged.values())
    for account in accounts:
        account["sources"].sort()
        account["source_count"] = len(account["sources"])
        observed_values = sorted(
            {
                str(record.get("observed_at") or "").strip()
                for record in account.get("source_records", [])
                if str(record.get("observed_at") or "").strip()
            }
        )
        if observed_values:
            observation_details = [
                {"label": "Last observed by Corvus", "value": observed_values[-1]}
            ]
            if len(observed_values) > 1 and observed_values[0] != observed_values[-1]:
                observation_details.insert(
                    0, {"label": "First observed by Corvus", "value": observed_values[0]}
                )
            account["details"] = _merge_details(account["details"], observation_details)
        account["richness"] = (
            sum(value not in (None, "") for value in account["profile"].values()) * 4
            + len(account["details"]) * 2
            + len(account["timeline_events"]) * 3
            + account["source_count"] * 3
            + STATUS_ORDER.get(account["status"], 0)
        )
        account["classification"] = (
            "corroborated"
            if account["source_count"] > 1
            else "rich"
            if account["richness"] >= 18
            else "lead"
        )
    return sorted(
        accounts,
        key=lambda item: (
            -STATUS_ORDER.get(item["status"], 0),
            -item["richness"],
            -item["source_count"],
            item["platform"].casefold(),
        ),
    )


def _add_entity(
    grouped: dict[str, dict[str, dict[str, Any]]],
    kind: str,
    value: Any,
    account: dict[str, Any],
) -> None:
    text = _text(value)
    if not text:
        return
    key = re.sub(r"\s+", " ", text).casefold()
    entry = grouped[kind].setdefault(
        key,
        {"value": text, "platforms": set(), "sources": set(), "accounts": set()},
    )
    entry["platforms"].add(account["platform"])
    entry["sources"].update(account["sources"])
    entry["accounts"].add(account["key"])


def extract_entities(accounts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Extract grouped values for the CLI entity panels."""
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for account in accounts:
        profile = account["profile"]
        _add_entity(grouped, "usernames", profile.get("account_id") or account.get("value"), account)
        _add_entity(grouped, "names", profile.get("display_name"), account)
        _add_entity(grouped, "locations", profile.get("location"), account)
        _add_entity(grouped, "images", profile.get("avatar_url"), account)

        for detail in account["details"]:
            label = detail["label"].casefold()
            if re.search(r"\bid\b", label) or "wallet address" in label:
                _add_entity(grouped, "ids", detail["value"], account)
            elif "username" in label or label == "nickname":
                values = [detail["value"]]
                if label == "known usernames":
                    quoted = re.findall(r"['\"]([^'\"]{1,128})['\"]", detail["value"])
                    values = quoted or [part.strip() for part in detail["value"].split(",")]
                for value in values:
                    _add_entity(grouped, "usernames", value, account)
            elif "email" in label and "verified" not in label:
                _add_entity(grouped, "emails", detail["value"], account)
            elif "image" in label:
                _add_entity(grouped, "images", detail["value"], account)

        for source_record in account["source_records"]:
            metadata = _metadata(source_record)
            for key, value in metadata.items():
                key_text = str(key).casefold()
                if "email" in key_text and "verified" not in key_text and isinstance(value, str):
                    if "@" in value or "***" in value:
                        _add_entity(grouped, "emails", value, account)
            for image_key in (
                "banner_url",
                "image_bg",
                "snapcode_image",
                "imgur_profile_avatar_url",
                "gravatar_url",
                "image",
                "picture",
            ):
                image_url = metadata.get(image_key)
                if image_url:
                    _add_entity(grouped, "images", image_url, account)

    output: dict[str, list[dict[str, Any]]] = {}
    for kind in ("usernames", "names", "emails", "ids", "locations", "images"):
        values = []
        for entry in grouped[kind].values():
            values.append(
                {
                    "value": entry["value"],
                    "platforms": sorted(entry["platforms"], key=str.casefold),
                    "sources": sorted(entry["sources"]),
                    "account_count": len(entry["accounts"]),
                    "account_keys": sorted(entry["accounts"]),
                }
            )
        output[kind] = sorted(
            values,
            key=lambda item: (-item["account_count"], str(item["value"]).casefold()),
        )
    return output


def build_excluded_timeline_observations(
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for account in accounts:
        for event in account["timeline_events"]:
            if event.get("include_in_timeline") is not False:
                continue
            observations.append(
                {
                    **event,
                    "account_key": account["key"],
                    "platform": account["platform"],
                    "url": account["url"],
                    "display_name": account["profile"].get("display_name"),
                    "username": account["profile"].get("account_id") or account["value"],
                    "sources": event.get("sources", account["sources"]),
                }
            )
    return sorted(observations, key=lambda event: event["timestamp"], reverse=True)


def build_timeline(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for account in accounts:
        for event in account["timeline_events"]:
            if event.get("include_in_timeline") is False:
                continue
            key = (account["key"], event["type"], event["timestamp"])
            if key in seen:
                continue
            seen.add(key)
            timeline.append(
                {
                    **event,
                    "account_key": account["key"],
                    "platform": account["platform"],
                    "url": account["url"],
                    "display_name": account["profile"].get("display_name"),
                    "username": account["profile"].get("account_id") or account["value"],
                    "sources": event.get("sources", account["sources"]),
                }
            )
    return sorted(timeline, key=lambda event: event["timestamp"], reverse=True)


def reparse_audit(root: Path) -> dict[str, Any]:
    """Re-run current normalisers over retained parsed records without network access."""
    run = _read_json(root / "run.json", {})
    if not isinstance(run, dict):
        raise ValueError("run.json does not contain an object")
    query = _text(run.get("query"))
    if not query:
        raise ValueError("run.json does not contain a query")
    try:
        query_type = QueryType(str(run.get("query_type", QueryType.USERNAME.value)))
    except ValueError as exc:
        raise ValueError("run.json contains an unsupported query type") from exc
    tools = run.get("tools")
    if not isinstance(tools, list):
        tools = []

    reparsed: dict[str, Any] = {"query": query, "query_type": query_type.value, "tools": {}}
    for tool in tools:
        tool_id = str(tool)
        tool_root = root / tool_id
        records = _read_json(tool_root / "parsed.json", [])
        if not isinstance(records, list):
            reparsed["tools"][tool_id] = {"state": "invalid_parsed_file", "count": 0}
            continue
        try:
            adapter = get_adapter(tool_id)
            findings = adapter.normalise_results(
                [item for item in records if isinstance(item, dict)],
                query,
                query_type,
                str(tool_root / "raw"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            reparsed["tools"][tool_id] = {
                "state": "failed",
                "count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        output = tool_root / "normalised-current.json"
        output.write_text(
            json.dumps(
                [finding.model_dump(mode="json") for finding in findings],
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        reparsed["tools"][tool_id] = {
            "state": "completed",
            "count": len(findings),
            "output": str(output),
        }
    return reparsed


def load_audit_records(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    run = _read_json(root / "run.json", {})
    summaries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    tools = run.get("tools") if isinstance(run, dict) else []
    if not isinstance(tools, list):
        tools = []
    if not tools:
        tools = sorted(path.name for path in root.iterdir() if path.is_dir())
    for tool in tools:
        tool_root = root / str(tool)
        summary = _read_json(tool_root / "summary.json", {})
        if isinstance(summary, dict) and summary:
            summaries.append(summary)
        current_path = tool_root / "normalised-current.json"
        normalised = _read_json(
            current_path if current_path.is_file() else tool_root / "normalised.json", []
        )
        if isinstance(normalised, list):
            records.extend(item for item in normalised if isinstance(item, dict))
    return run if isinstance(run, dict) else {}, summaries, records


def build_audit_presentation(root: Path) -> dict[str, Any]:
    run, collector_summaries, records = load_audit_records(root)
    accounts = merge_accounts(records)
    entities = extract_entities(accounts)
    parsing_gaps = analyse_parsing_gaps(records)
    timeline = build_timeline(accounts)
    excluded_timeline_observations = build_excluded_timeline_observations(accounts)
    status_counts: dict[str, int] = defaultdict(int)
    classification_counts: dict[str, int] = defaultdict(int)
    platform_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "found": 0, "corroborated": 0, "rich": 0}
    )
    for account in accounts:
        status_counts[account["status"]] += 1
        classification_counts[account["classification"]] += 1
        platform = platform_counts[account["platform"]]
        platform["total"] += 1
        platform["found"] += account["status"] == "found"
        platform["corroborated"] += account["source_count"] > 1
        platform["rich"] += account["richness"] >= 18
    platform_summary = [
        {"platform": name, **counts}
        for name, counts in sorted(
            platform_counts.items(),
            key=lambda item: (-item[1]["found"], -item[1]["rich"], item[0].casefold()),
        )
    ]
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "audit_root": str(root),
        "query": run.get("query"),
        "query_type": run.get("query_type", "username"),
        "methodology": {
            "deduplication_scope": "same platform and account identifier",
            "identity_warning": (
                "Matching usernames across different platforms are retained as separate account leads; "
                "the renderer does not assert that they belong to the same person."
            ),
        },
        "summary": {
            "collector_count": len(collector_summaries),
            "raw_record_count": len(records),
            "account_count": len(accounts),
            "status_counts": dict(sorted(status_counts.items())),
            "classification_counts": dict(sorted(classification_counts.items())),
            "corroborated_account_count": sum(item["source_count"] > 1 for item in accounts),
            "rich_account_count": sum(item["richness"] >= 18 for item in accounts),
            "timeline_event_count": len(timeline),
            "excluded_timeline_observation_count": len(excluded_timeline_observations),
            "entity_counts": {key: len(value) for key, value in entities.items()},
            "parser_gap_field_count": parsing_gaps["field_count"],
            "high_priority_parser_gap_count": parsing_gaps["high_priority_count"],
        },
        "collectors": collector_summaries,
        "accounts": accounts,
        "platform_summary": platform_summary,
        "entities": entities,
        "parsing_gaps": parsing_gaps,
        "timeline": timeline,
        "excluded_timeline_observations": excluded_timeline_observations,
    }


def render_html(presentation: dict[str, Any], *, card_limit: int = 1000) -> str:
    """Render a self-contained, autoescaped HTML review without loading remote assets."""
    template_root = Path(__file__).resolve().parents[1] / "templates"
    environment = Environment(
        loader=FileSystemLoader(template_root),
        autoescape=select_autoescape(("html", "xml")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("collector_audit.html")
    bounded = dict(presentation)
    accounts = presentation.get("accounts", [])
    bounded["accounts"] = accounts[: max(1, min(card_limit, 5000))]
    bounded["rendered_account_count"] = len(bounded["accounts"])
    bounded["total_account_count"] = len(accounts)
    return template.render(presentation=bounded)


def _one_line(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def render_text(presentation: dict[str, Any], *, card_limit: int = 40) -> str:
    """Render a compact terminal/file summary with account cards and entity panels."""
    summary = presentation["summary"]
    lines = [
        f"OSINT collector audit — {presentation.get('query') or 'unknown query'}",
        "=" * 72,
        (
            f"{summary['collector_count']} collectors · {summary['raw_record_count']} raw records · "
            f"{summary['account_count']} merged accounts · "
            f"{summary['corroborated_account_count']} corroborated · "
            f"{summary['timeline_event_count']} timeline events · "
            f"{summary['excluded_timeline_observation_count']} excluded observations · "
            f"{summary['parser_gap_field_count']} parser-gap fields"
        ),
        presentation["methodology"]["identity_warning"],
        "",
        "COLLECTORS",
        "----------",
    ]
    for collector in presentation["collectors"]:
        lines.append(
            f"{collector.get('display_name') or collector.get('tool')}: "
            f"{collector.get('state', 'unknown')} · "
            f"{collector.get('normalised_count', 0)} records · "
            f"{collector.get('found_count', 0)} found · "
            f"{collector.get('unknown_count', 0)} unknown"
        )
        if collector.get("error_message"):
            lines.append(f"  Error: {_one_line(collector['error_message'])}")

    lines.extend(("", "PARSER GAP REVIEW", "-----------------"))
    if presentation["parsing_gaps"]["fields"]:
        for item in presentation["parsing_gaps"]["fields"][:40]:
            platforms = ", ".join(item["platforms"][:6])
            example = item["examples"][0]["value"] if item["examples"] else ""
            lines.append(
                f"{item['priority'].upper()} · {item['field']} · {item['occurrences']} occurrence(s) · "
                f"{platforms} · example {_one_line(example, 140)}"
            )
        remaining = len(presentation["parsing_gaps"]["fields"]) - 40
        if remaining > 0:
            lines.append(f"… {remaining} more parser-gap fields")
    else:
        lines.append("No likely identity-bearing parser gaps were detected.")

    lines.extend(("", f"ACCOUNT CARDS (top {min(card_limit, len(presentation['accounts']))})", "-------------"))
    for account in presentation["accounts"][:card_limit]:
        profile = account["profile"]
        name = profile.get("display_name") or profile.get("account_id") or account.get("value") or "Unnamed"
        lines.append(
            f"┌ {account['platform']} · {account['status']} · {account['classification']} · "
            f"sources: {', '.join(account['sources'])}"
        )
        lines.append(f"│ {_one_line(name)}")
        if account.get("url"):
            lines.append(f"│ {account['url']}")
        if profile.get("avatar_url"):
            lines.append(f"│ Profile image: {_one_line(profile['avatar_url'], 220)}")
        facts = []
        if profile.get("account_id"):
            facts.append(f"username {profile['account_id']}")
        if profile.get("location"):
            facts.append(f"location {profile['location']}")
        if profile.get("followers") is not None:
            facts.append(f"{profile['followers']:,} followers")
        if profile.get("following") is not None:
            facts.append(f"{profile['following']:,} following")
        if profile.get("verified"):
            facts.append("verified")
        if facts:
            lines.append(f"│ {' · '.join(facts)}")
        if profile.get("biography"):
            lines.append(f"│ {_one_line(profile['biography'])}")
        if account["details"]:
            detail_text = " · ".join(
                f"{item['label']}: {item['value']}" for item in account["details"][:8]
            )
            lines.append(f"│ {_one_line(detail_text, 300)}")
        included_events = [
            item for item in account["timeline_events"] if item.get("include_in_timeline") is not False
        ]
        excluded_events = [
            item for item in account["timeline_events"] if item.get("include_in_timeline") is False
        ]
        if included_events:
            event_text = " · ".join(
                f"{item['label']}: {item['timestamp']}" for item in included_events[:3]
            )
            lines.append(f"│ {_one_line(event_text, 300)}")
        for item in excluded_events[:2]:
            reason = item.get("note") or "Excluded because its meaning is not reliable enough."
            lines.append(
                f"│ Excluded observation — {item['label']}: {item['timestamp']} · "
                f"{_one_line(reason, 220)}"
            )
        lines.append("└" + "─" * 71)

    lines.extend(("", "GROUPED ENTITIES", "----------------"))
    for kind, values in presentation["entities"].items():
        lines.append(f"{kind.title()} ({len(values)}):")
        for item in values[:20]:
            platforms = ", ".join(item["platforms"][:6])
            lines.append(
                f"  - {_one_line(item['value'], 120)} · {item['account_count']} account(s) · {platforms}"
            )
        if len(values) > 20:
            lines.append(f"  … {len(values) - 20} more")

    lines.extend(("", "COMBINED TIMELINE", "-----------------"))
    for event in presentation["timeline"]:
        identity = event.get("display_name") or event.get("username") or "account"
        lines.append(
            f"{event['timestamp']} · {event['platform']} · {event['label']} · {_one_line(identity, 80)}"
        )
    if not presentation["timeline"]:
        lines.append("No explicit dated events were parsed.")
    return "\n".join(lines) + "\n"

from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.core.security import canonicalise_url
from app.models.db import Investigation

MAX_HISTORICAL_URLS = 250
MAX_QUERY_KEYS = 50


def build_historical_url_view(investigation: Investigation) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    query_keys: defaultdict[str, int] = defaultdict(int)

    def add(url_value: object, source: str, observed_at: object = None, metadata: dict[str, Any] | None = None) -> None:
        canonical = canonicalise_url(str(url_value or ""))
        if not canonical:
            return
        item = grouped.setdefault(
            canonical,
            {
                "url": canonical,
                "host": urlsplit(canonical).hostname or "",
                "path": urlsplit(canonical).path or "/",
                "sources": [],
                "observations": [],
                "metadata": {},
            },
        )
        if source not in item["sources"]:
            item["sources"].append(source)
        if observed_at:
            observation = {"source": source, "observed_at": str(observed_at)[:64]}
            if observation not in item["observations"]:
                item["observations"].append(observation)
        if metadata:
            item["metadata"][source] = metadata
        for key, _value in parse_qsl(urlsplit(canonical).query, keep_blank_values=True):
            if key:
                query_keys[key[:100]] += 1

    for finding in investigation.findings:
        if finding.finding_type == "historical_url":
            observed = (
                finding.extra.get("crawl_observed_at")
                or finding.extra.get("otx_observed_at")
                or finding.extra.get("observed_at")
            )
            source = finding.platform or finding.source_tool
            add(finding.canonical_url or finding.value or finding.display_url, source, observed, finding.extra)

        wayback = finding.extra.get("wayback") if isinstance(finding.extra, dict) else None
        if not isinstance(wayback, dict):
            continue
        records = wayback.get("cdx_records")
        if not isinstance(records, list):
            continue
        for record in records[:50]:
            if not isinstance(record, dict):
                continue
            add(
                record.get("original_url") or wayback.get("target_url"),
                "Wayback Machine",
                record.get("captured_at"),
                {"archive_url": record.get("archive_url"), "digest": record.get("digest")},
            )

    rows = list(grouped.values())
    for row in rows:
        row["sources"].sort()
        row["observations"].sort(key=lambda value: value.get("observed_at", ""))
        row["first_observed_at"] = row["observations"][0]["observed_at"] if row["observations"] else None
        row["last_observed_at"] = row["observations"][-1]["observed_at"] if row["observations"] else None
    rows.sort(key=lambda value: (value["last_observed_at"] or "", value["url"]), reverse=True)

    source_counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        for source in row["sources"]:
            source_counts[source] += 1

    return {
        "enabled": bool(rows),
        "urls": rows[:MAX_HISTORICAL_URLS],
        "total_url_count": len(rows),
        "source_counts": dict(sorted(source_counts.items())),
        "query_keys": [
            {"key": key, "count": count}
            for key, count in sorted(query_keys.items(), key=lambda item: (-item[1], item[0]))[:MAX_QUERY_KEYS]
        ],
        "truncated": len(rows) > MAX_HISTORICAL_URLS,
        "methodology": "Historical URL observations are passive index/scan/archive records. They do not prove that a URL is currently live or that its content is unchanged.",
    }

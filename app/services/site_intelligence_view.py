from __future__ import annotations

from collections import Counter
from typing import Any

from app.models.db import Investigation

SITE_FINDING_TYPES = frozenset(
    {
        "email",
        "document_url",
        "linked_social_profile",
        "external_domain",
        "javascript_endpoint",
        "tracking_identifier",
        "technology_hint",
    }
)
MAX_VISIBLE_PER_GROUP = 40


def build_site_intelligence_view(investigation: Investigation) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {kind: [] for kind in SITE_FINDING_TYPES}
    counts: Counter[str] = Counter()
    for finding in investigation.findings:
        if finding.source_tool != "site_intelligence" or finding.finding_type not in SITE_FINDING_TYPES:
            continue
        counts[finding.finding_type] += 1
        bucket = groups[finding.finding_type]
        if len(bucket) >= MAX_VISIBLE_PER_GROUP:
            continue
        bucket.append(
            {
                "id": finding.id,
                "value": finding.value,
                "url": finding.canonical_url or finding.display_url,
                "description": finding.description,
                "source_url": finding.extra.get("source_url"),
                "tracking_type": finding.extra.get("tracking_type"),
                "hint_type": finding.extra.get("hint_type"),
                "script_url": finding.extra.get("script_url"),
            }
        )
    total = sum(counts.values())
    return {
        "enabled": total > 0,
        "total": total,
        "counts": dict(counts),
        "groups": groups,
        "methodology": (
            "Live Site Intelligence is a bounded, robots-aware same-domain crawl. It performs GET-only "
            "public retrieval, does not submit forms, fuzz parameters, attempt login, hunt secrets or crawl "
            "outbound domains. Extracted emails, social links, documents, JavaScript endpoint strings, "
            "tracking identifiers and declared technology hints retain the page or script where they were observed."
        ),
    }

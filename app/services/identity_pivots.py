import re
from dataclasses import dataclass
from typing import Any

from app.models.db import Investigation
from app.models.schemas import FindingStatus

GENERIC_DISPLAY_NAMES = frozenset(
    {
        "facebook",
        "github",
        "instagram",
        "linkedin",
        "reddit",
        "snapchat",
        "tiktok",
        "twitch",
        "twitter",
        "x",
        "youtube",
    }
)
MAX_DISPLAY_NAME_PIVOTS = 5


@dataclass(frozen=True, slots=True)
class DisplayNamePivot:
    value: str
    finding_ids: tuple[str, ...]
    platforms: tuple[str, ...]
    source_tools: tuple[str, ...]


def normalise_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = re.sub(r"\s+", " ", value).strip(" \t\r\n|·•-_.,")
    if not 3 <= len(candidate) <= 120:
        return None
    if candidate.casefold() in GENERIC_DISPLAY_NAMES:
        return None
    if "@" in candidate or "://" in candidate:
        return None
    words = re.findall(r"[\w'’-]+", candidate, flags=re.UNICODE)
    if len(words) < 2 or sum(character.isalpha() for character in candidate) < 4:
        return None
    return candidate


def discover_display_name_pivots(investigation: Investigation) -> list[DisplayNamePivot]:
    grouped: dict[str, dict[str, Any]] = {}
    query_key = investigation.query.casefold()
    for finding in investigation.findings:
        if finding.status not in {FindingStatus.FOUND.value, FindingStatus.LIKELY.value}:
            continue
        profile = finding.extra.get("profile", {})
        if not isinstance(profile, dict):
            continue
        display_name = normalise_display_name(profile.get("display_name"))
        if display_name is None:
            continue
        key = display_name.casefold()
        if key in {
            query_key,
            str(finding.value or "").casefold(),
            str(finding.platform or "").casefold(),
        }:
            continue
        entry = grouped.setdefault(
            key,
            {
                "value": display_name,
                "finding_ids": set(),
                "platforms": set(),
                "source_tools": set(),
            },
        )
        entry["finding_ids"].add(finding.id)
        if finding.platform:
            entry["platforms"].add(finding.platform)
        entry["source_tools"].update(finding.source_tools or [finding.source_tool])

    pivots = [
        DisplayNamePivot(
            value=entry["value"],
            finding_ids=tuple(sorted(entry["finding_ids"])),
            platforms=tuple(sorted(entry["platforms"])),
            source_tools=tuple(sorted(entry["source_tools"])),
        )
        for entry in grouped.values()
    ]
    pivots.sort(
        key=lambda item: (
            -len(item.finding_ids),
            -len(item.source_tools),
            item.value.casefold(),
        )
    )
    return pivots[:MAX_DISPLAY_NAME_PIVOTS]

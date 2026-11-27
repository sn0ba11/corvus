"""Additional analyst-supplied identifiers searched inside one case.

A case keeps one primary search value. Anything else the analyst already knows about the
subject (another username, an email address, a full name, a phone number, a domain) is
queued as its own depth-zero collector runs in the same case, so every identifier gets the
collectors that suit its type and all evidence lands in one register.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.adapters import REGISTRY
from app.core.upstreams import commit_for
from app.models.db import AuditEvent, Investigation, ToolRun
from app.models.schemas import QueryType, RunStatus
from app.services.tool_settings import get_tool_timeouts

MAX_ADDITIONAL_IDENTIFIERS = 8
MAX_IDENTIFIER_LENGTH = 320
_PREFIXES = {
    "username": QueryType.USERNAME,
    "user": QueryType.USERNAME,
    "handle": QueryType.USERNAME,
    "name": QueryType.NAME,
    "email": QueryType.EMAIL,
    "phone": QueryType.PHONE,
    "domain": QueryType.DOMAIN,
}


@dataclass(frozen=True, slots=True)
class IdentifierSeed:
    value: str
    query_type: QueryType

    @property
    def key(self) -> tuple[str, str]:
        return self.query_type.value, self.value.casefold()


def parse_additional_identifiers(
    text: str,
    detect: Callable[[str], QueryType | None],
    validate: Callable[[str, QueryType], None],
) -> list[IdentifierSeed]:
    """Parse one identifier per line, with an optional ``type:`` prefix to force the type."""
    seeds: dict[tuple[str, str], IdentifierSeed] = {}
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip().strip(",;")
        if not line:
            continue
        if len(line) > MAX_IDENTIFIER_LENGTH:
            raise ValueError("an additional identifier is too long")
        query_type: QueryType | None = None
        prefix, separator, remainder = line.partition(":")
        if separator and prefix.strip().casefold() in _PREFIXES and remainder.strip():
            query_type = _PREFIXES[prefix.strip().casefold()]
            line = remainder.strip()
        if query_type is None and line.startswith("@"):
            query_type, line = QueryType.USERNAME, line.lstrip("@")
        if query_type is None:
            query_type = detect(line)
        if query_type is None:
            raise ValueError(
                f"could not tell what kind of identifier '{line[:60]}' is; "
                "prefix it with username:, name:, email:, phone: or domain:"
            )
        if query_type == QueryType.USERNAME:
            line = line.lstrip("@")
        validate(line, query_type)
        seed = IdentifierSeed(value=line, query_type=query_type)
        seeds.setdefault(seed.key, seed)
    if len(seeds) > MAX_ADDITIONAL_IDENTIFIERS:
        raise ValueError(
            f"add no more than {MAX_ADDITIONAL_IDENTIFIERS} additional identifiers at a time"
        )
    return list(seeds.values())


def case_seeds(investigation: Investigation) -> list[IdentifierSeed]:
    """The primary search value followed by every analyst-supplied additional identifier."""
    primary = IdentifierSeed(
        value=investigation.query.strip(), query_type=QueryType(investigation.query_type)
    )
    seeds: dict[tuple[str, str], IdentifierSeed] = {primary.key: primary}
    for run in investigation.runs:
        if (
            not run.query_value
            or not run.query_type
            or run.pivot_depth != 0
            or run.pivot_automatic
            or run.pivot_source_finding_id
        ):
            continue
        seed = IdentifierSeed(value=run.query_value, query_type=QueryType(run.query_type))
        seeds.setdefault(seed.key, seed)
    return list(seeds.values())


def default_tools_for(query_type: QueryType) -> list[str]:
    return [
        tool_id
        for tool_id, adapter in REGISTRY.items()
        if query_type in adapter.supported_query_types
        and adapter.default_selected
        and tool_id != "metasearch2"
    ]


def queue_additional_identifiers(
    db: Session, investigation: Investigation, seeds: list[IdentifierSeed]
) -> list[ToolRun]:
    """Queue the default collectors for each identifier not already searched in this case."""
    known = {seed.key for seed in case_seeds(investigation)}
    timeouts = get_tool_timeouts(db)
    queued: list[ToolRun] = []
    for seed in seeds:
        if seed.key in known:
            continue
        known.add(seed.key)
        tools = default_tools_for(seed.query_type)
        for tool_id in tools:
            run = ToolRun(
                investigation_id=investigation.id,
                tool_id=tool_id,
                display_name=REGISTRY[tool_id].display_name,
                status=RunStatus.QUEUED.value,
                timeout_seconds=timeouts[tool_id],
                tool_commit=commit_for(tool_id),
                query_value=seed.value,
                query_type=seed.query_type.value,
                pivot_depth=0,
                pivot_source_finding_id=None,
                pivot_automatic=False,
            )
            db.add(run)
            queued.append(run)
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="additional_identifier_queued",
                details={
                    "query_type": seed.query_type.value,
                    "value": seed.value,
                    "tools": tools,
                },
            )
        )
    return queued

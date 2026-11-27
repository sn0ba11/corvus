import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.adapters import REGISTRY
from app.core.upstreams import commit_for
from app.models.db import AuditEvent, Finding, Investigation, ToolRun
from app.models.schemas import FindingStatus, QueryType, RunStatus
from app.services.handle_candidates import HandleCandidate
from app.services.profile_links import profile_account_from_url
from app.services.tool_settings import get_tool_timeouts

USERNAME_RE = re.compile(r"^[\w.\-]{3,64}$", re.UNICODE)
USERNAME_KEYS = frozenset({"username", "user_name", "login", "handle", "screen_name"})
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,253}$")
EMAIL_KEYS = frozenset(
    {"email", "email_address", "public_email", "contact_email", "recovery_email"}
)
PIVOT_TOOLS = (
    # Exact public-profile lookups: one or two requests each, so cheap to repeat per handle.
    "username_public_profiles",
    "snapchat_public",
    "telegram_public",
    "tiktok_public",
    "maigret",
    "sherlock",
    "blackbird",
    "instaloader",
    "nexfil",
    "social_analyzer",
)
EMAIL_PIVOT_TOOLS = ("gravatar_public", "pgp_public", "holehe", "blackbird")
MAX_USERNAME_PIVOTS = 5  # automatic follow bound
MAX_USERNAME_PIVOT_CANDIDATES = 20  # candidates listed for the analyst
SEARCH_RESULT_REASON = "search_result.account_page"
POST_LINK_REASON_PREFIX = "post_link."
# Collectors whose findings are search-engine results rather than a look at the account itself.
SEARCH_SOURCE_TOOLS = frozenset(
    {"search_browser_capture", "search_import", "searxng_name_search", "bing_name_search",
     "metasearch2", "duckduckgo_instant", "commoncrawl"}
)
MAX_EMAIL_PIVOTS = 5  # automatic follow bound
MAX_EMAIL_PIVOT_CANDIDATES = 20  # candidates listed for the analyst
MAX_PIVOT_DEPTH = 2
ROLE_ADDRESS_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "admin",
        "billing",
        "contact",
        "help",
        "hostmaster",
        "info",
        "marketing",
        "noc",
        "no-reply",
        "noreply",
        "postmaster",
        "sales",
        "security",
        "support",
        "webmaster",
    }
)


@dataclass(frozen=True, slots=True)
class UsernamePivotCandidate:
    username: str
    finding_ids: tuple[str, ...]
    source_tools: tuple[str, ...]
    platforms: tuple[str, ...]
    reasons: tuple[str, ...]
    followed: bool


@dataclass(frozen=True, slots=True)
class EmailPivotCandidate:
    email: str
    finding_ids: tuple[str, ...]
    source_tools: tuple[str, ...]
    platforms: tuple[str, ...]
    reasons: tuple[str, ...]
    followed: bool


def normalise_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().removeprefix("@").strip()
    if not candidate or "@" in candidate or "://" in candidate or candidate.isdecimal():
        return None
    if not USERNAME_RE.fullmatch(candidate):
        return None
    return candidate


def normalise_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if "*" in candidate or not EMAIL_RE.fullmatch(candidate):
        return None
    local, domain = candidate.rsplit("@", 1)
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return f"{local}@{domain.casefold()}"


def _explicit_username_values(value: Any, *, depth: int = 0) -> list[tuple[str, str]]:
    if depth > 3 or not isinstance(value, dict):
        return []
    found: list[tuple[str, str]] = []
    for key, item in value.items():
        normalised_key = str(key).casefold()
        if normalised_key in USERNAME_KEYS:
            username = normalise_username(item)
            if username:
                found.append((username, f"metadata.{normalised_key}"))
        elif isinstance(item, dict):
            found.extend(_explicit_username_values(item, depth=depth + 1))
        elif isinstance(item, list) and depth < 2:
            for child in item[:25]:
                if isinstance(child, dict):
                    found.extend(_explicit_username_values(child, depth=depth + 1))
    return found


def _explicit_email_values(value: Any, *, depth: int = 0) -> list[tuple[str, str]]:
    if depth > 3 or not isinstance(value, dict):
        return []
    found: list[tuple[str, str]] = []
    for key, item in value.items():
        normalised_key = str(key).casefold()
        if normalised_key in EMAIL_KEYS:
            email = normalise_email(item)
            if email:
                found.append((email, f"metadata.{normalised_key}"))
        elif isinstance(item, dict):
            found.extend(_explicit_email_values(item, depth=depth + 1))
        elif isinstance(item, list) and depth < 2:
            for child in item[:25]:
                if isinstance(child, dict):
                    found.extend(_explicit_email_values(child, depth=depth + 1))
    return found


def _finding_username_values(finding: Finding) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    profile = finding.extra.get("profile", {})
    if isinstance(profile, dict):
        account_id = normalise_username(profile.get("account_id"))
        if account_id:
            values.append((account_id, "profile.account_id"))
    values.extend(_explicit_username_values(finding.extra))
    # Websites the account owner lists (Keybase proofs, Lichess links, Snapchat same-as links,
    # links in Telegram posts) often point straight at another account of theirs.
    linked_sites = finding.extra.get("linked_sites")
    for site in linked_sites[:20] if isinstance(linked_sites, list) else []:
        account = profile_account_from_url(site) if isinstance(site, str) else None
        username = normalise_username(account[1]) if account else None
        if username:
            values.append((username, f"linked_site.{account[0].casefold()}" if account else "linked_site"))
    # Say how the handle was learned: a search hit and a link inside a channel post are weaker
    # than something the account's own profile states.
    if finding.finding_type == "search_result":
        values = [(username, SEARCH_RESULT_REASON) for username, _ in values]
    elif finding.finding_type == "telegram_public_profile":
        values = [
            (username, reason.replace("linked_site.", POST_LINK_REASON_PREFIX, 1))
            for username, reason in values
        ]
    deduplicated: dict[str, tuple[str, str]] = {}
    for username, reason in values:
        deduplicated.setdefault(username.casefold(), (username, reason))
    return list(deduplicated.values())


def _reason_rank(reasons: set[str]) -> int:
    """0 for profile-stated handles, 1 for search hits only, 2 for post links only."""
    if any(r != SEARCH_RESULT_REASON and not r.startswith(POST_LINK_REASON_PREFIX) for r in reasons):
        return 0
    return 1 if SEARCH_RESULT_REASON in reasons else 2


def _finding_email_values(finding: Finding) -> list[tuple[str, str]]:
    values = _explicit_email_values(finding.extra)
    if finding.finding_type.casefold() == "email":
        email = normalise_email(finding.value)
        if email:
            values.append((email, "finding.value"))
    deduplicated: dict[str, tuple[str, str]] = {}
    for email, reason in values:
        deduplicated.setdefault(email.casefold(), (email, reason))
    return list(deduplicated.values())


def _is_role_address_local_part(value: str) -> bool:
    return value.casefold() in ROLE_ADDRESS_LOCAL_PARTS


def _automatic_candidate_is_corroborated(candidate: UsernamePivotCandidate) -> bool:
    # Two search collectors returning the same URL are one observation, not corroboration,
    # and a link in a channel post says nothing about who owns the linked account.
    if _reason_rank(set(candidate.reasons)) > 0:
        return False
    independent_tools = len(set(candidate.source_tools) - SEARCH_SOURCE_TOOLS)
    local_part_support = "email.local_part" in candidate.reasons
    return independent_tools >= 2 or (local_part_support and independent_tools >= 1)


def _source_depth_for_finding_ids(
    investigation: Investigation, finding_ids: tuple[str, ...]
) -> int:
    if not finding_ids:
        return 0
    source_ids = set(finding_ids)
    depths = [
        run.pivot_depth
        for run in investigation.runs
        if run.findings and any(finding.id in source_ids for finding in run.findings)
    ]
    return max(depths, default=0)


def discover_username_pivots(investigation: Investigation) -> list[UsernamePivotCandidate]:
    followed = {
        run.query_value.casefold()
        for run in investigation.runs
        if run.query_type == QueryType.USERNAME.value and run.query_value
    }
    grouped: dict[str, dict[str, Any]] = {}
    local_part = (
        investigation.query.rsplit("@", 1)[0].split("+", 1)[0]
        if investigation.query_type == QueryType.EMAIL.value
        else ""
    )
    local_username = normalise_username(local_part)
    if local_username and not _is_role_address_local_part(local_username):
        grouped[local_username.casefold()] = {
            "username": local_username,
            "finding_ids": set(),
            "source_tools": set(),
            "platforms": {"Email address"},
            "reasons": {"email.local_part"},
        }
    for finding in investigation.findings:
        if finding.status not in {FindingStatus.FOUND.value, FindingStatus.LIKELY.value}:
            continue
        for username, reason in _finding_username_values(finding):
            key = username.casefold()
            entry = grouped.setdefault(
                key,
                {
                    "username": username,
                    "finding_ids": set(),
                    "source_tools": set(),
                    "platforms": set(),
                    "reasons": set(),
                },
            )
            entry["finding_ids"].add(finding.id)
            entry["source_tools"].update(finding.source_tools or [finding.source_tool])
            if finding.platform:
                entry["platforms"].add(finding.platform)
            entry["reasons"].add(reason)

    candidates = [
        UsernamePivotCandidate(
            username=entry["username"],
            finding_ids=tuple(sorted(entry["finding_ids"])),
            source_tools=tuple(sorted(entry["source_tools"])),
            platforms=tuple(sorted(entry["platforms"])),
            reasons=tuple(sorted(entry["reasons"])),
            followed=key in followed,
        )
        for key, entry in grouped.items()
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate.followed,
            _reason_rank(set(candidate.reasons)),
            -len(candidate.finding_ids),
            -len(candidate.source_tools),
            candidate.username.casefold(),
        )
    )
    return candidates[:MAX_USERNAME_PIVOT_CANDIDATES]


def queue_username_pivot(
    db: Session,
    investigation: Investigation,
    candidate: UsernamePivotCandidate,
    *,
    automatic: bool,
) -> list[ToolRun]:
    if candidate.followed:
        return []
    source_depth = _source_depth_for_finding_ids(investigation, candidate.finding_ids)
    next_depth = source_depth + 1
    if next_depth > MAX_PIVOT_DEPTH:
        return []

    queued: list[ToolRun] = []
    configured_timeouts = get_tool_timeouts(db)
    source_finding_id = candidate.finding_ids[0] if candidate.finding_ids else None
    for tool_id in PIVOT_TOOLS:
        adapter = REGISTRY[tool_id]
        if QueryType.USERNAME not in adapter.supported_query_types:
            continue
        run = ToolRun(
            investigation_id=investigation.id,
            tool_id=tool_id,
            display_name=adapter.display_name,
            status=RunStatus.QUEUED.value,
            timeout_seconds=configured_timeouts[tool_id],
            tool_commit=commit_for(tool_id),
            query_value=candidate.username,
            query_type=QueryType.USERNAME.value,
            pivot_depth=next_depth,
            pivot_source_finding_id=source_finding_id,
            pivot_automatic=automatic,
        )
        db.add(run)
        queued.append(run)
    if queued:
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="username_pivot_queued",
                details={
                    "username": candidate.username,
                    "automatic": automatic,
                    "pivot_depth": next_depth,
                    "source_finding_ids": list(candidate.finding_ids),
                    "source_tools": list(candidate.source_tools),
                    "platforms": list(candidate.platforms),
                    "reasons": list(candidate.reasons),
                    "tools": [run.tool_id for run in queued],
                },
            )
        )
    return queued


def queue_seeded_username_sweep(
    db: Session,
    investigation: Investigation,
    candidate: HandleCandidate,
) -> list[ToolRun]:
    if investigation.query_type != QueryType.NAME.value:
        raise ValueError("seeded handle sweeps are available only for name investigations")
    username = normalise_username(candidate.username)
    if username is None:
        raise ValueError("invalid seeded username")
    if any(
        run.query_type == QueryType.USERNAME.value
        and (run.query_value or "").casefold() == username.casefold()
        for run in investigation.runs
    ):
        return []

    configured_timeouts = get_tool_timeouts(db)
    queued: list[ToolRun] = []
    for tool_id in PIVOT_TOOLS:
        adapter = REGISTRY[tool_id]
        if QueryType.USERNAME not in adapter.supported_query_types:
            continue
        run = ToolRun(
            investigation_id=investigation.id,
            tool_id=tool_id,
            display_name=adapter.display_name,
            status=RunStatus.QUEUED.value,
            timeout_seconds=configured_timeouts[tool_id],
            tool_commit=commit_for(tool_id),
            query_value=username,
            query_type=QueryType.USERNAME.value,
            pivot_depth=0,
            pivot_source_finding_id=None,
            pivot_automatic=False,
        )
        db.add(run)
        queued.append(run)
    if queued:
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="name_handle_seed_queued",
                details={
                    "username": username,
                    "source_name": candidate.source_name,
                    "provenance": candidate.provenance,
                    "automatic": False,
                    "tools": [run.tool_id for run in queued],
                },
            )
        )
    return queued


def queue_automatic_username_pivots(db: Session, investigation: Investigation) -> list[ToolRun]:
    if not investigation.auto_follow_username_pivots:
        return []
    queued: list[ToolRun] = []
    for candidate in discover_username_pivots(investigation)[:MAX_USERNAME_PIVOTS]:
        if candidate.followed or not _automatic_candidate_is_corroborated(candidate):
            continue
        queued.extend(queue_username_pivot(db, investigation, candidate, automatic=True))
    return queued


def discover_email_pivots(investigation: Investigation) -> list[EmailPivotCandidate]:
    followed = {
        run.query_value.casefold()
        for run in investigation.runs
        if run.query_type == QueryType.EMAIL.value and run.query_value
    }
    grouped: dict[str, dict[str, Any]] = {}
    for finding in investigation.findings:
        if finding.status not in {FindingStatus.FOUND.value, FindingStatus.LIKELY.value}:
            continue
        for email, reason in _finding_email_values(finding):
            key = email.casefold()
            entry = grouped.setdefault(
                key,
                {
                    "email": email,
                    "finding_ids": set(),
                    "source_tools": set(),
                    "platforms": set(),
                    "reasons": set(),
                },
            )
            entry["finding_ids"].add(finding.id)
            entry["source_tools"].update(finding.source_tools or [finding.source_tool])
            if finding.platform:
                entry["platforms"].add(finding.platform)
            entry["reasons"].add(reason)
    candidates = [
        EmailPivotCandidate(
            email=entry["email"],
            finding_ids=tuple(sorted(entry["finding_ids"])),
            source_tools=tuple(sorted(entry["source_tools"])),
            platforms=tuple(sorted(entry["platforms"])),
            reasons=tuple(sorted(entry["reasons"])),
            followed=key in followed,
        )
        for key, entry in grouped.items()
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate.followed,
            _reason_rank(set(candidate.reasons)),
            -len(candidate.finding_ids),
            -len(candidate.source_tools),
            candidate.email.casefold(),
        )
    )
    return candidates[:MAX_EMAIL_PIVOT_CANDIDATES]


def queue_email_pivot(
    db: Session,
    investigation: Investigation,
    candidate: EmailPivotCandidate,
    *,
    automatic: bool = False,
) -> list[ToolRun]:
    if candidate.followed:
        return []
    source_depth = _source_depth_for_finding_ids(investigation, candidate.finding_ids)
    next_depth = source_depth + 1
    if next_depth > MAX_PIVOT_DEPTH:
        return []
    configured_timeouts = get_tool_timeouts(db)
    source_finding_id = candidate.finding_ids[0] if candidate.finding_ids else None
    queued: list[ToolRun] = []
    for tool_id in EMAIL_PIVOT_TOOLS:
        adapter = REGISTRY[tool_id]
        if QueryType.EMAIL not in adapter.supported_query_types:
            continue
        run = ToolRun(
            investigation_id=investigation.id,
            tool_id=tool_id,
            display_name=adapter.display_name,
            status=RunStatus.QUEUED.value,
            timeout_seconds=configured_timeouts[tool_id],
            tool_commit=commit_for(tool_id),
            query_value=candidate.email,
            query_type=QueryType.EMAIL.value,
            pivot_depth=next_depth,
            pivot_source_finding_id=source_finding_id,
            pivot_automatic=automatic,
        )
        db.add(run)
        queued.append(run)
    if queued:
        db.add(
            AuditEvent(
                investigation_id=investigation.id,
                event_type="email_pivot_queued",
                details={
                    "email": candidate.email,
                    "automatic": automatic,
                    "pivot_depth": next_depth,
                    "source_finding_ids": list(candidate.finding_ids),
                    "source_tools": list(candidate.source_tools),
                    "platforms": list(candidate.platforms),
                    "reasons": list(candidate.reasons),
                    "tools": [run.tool_id for run in queued],
                },
            )
        )
    return queued


def _automatic_email_candidate_is_corroborated(candidate: EmailPivotCandidate) -> bool:
    # Email pivots can expose account-presence information, so only recurse when
    # the same explicit public address is independently observed by two collectors.
    return len(set(candidate.source_tools)) >= 2


def queue_automatic_email_pivots(db: Session, investigation: Investigation) -> list[ToolRun]:
    if not investigation.auto_follow_username_pivots:
        return []
    queued: list[ToolRun] = []
    for candidate in discover_email_pivots(investigation)[:MAX_EMAIL_PIVOTS]:
        if candidate.followed or not _automatic_email_candidate_is_corroborated(candidate):
            continue
        queued.extend(queue_email_pivot(db, investigation, candidate, automatic=True))
    return queued


def queue_automatic_identity_pivots(db: Session, investigation: Investigation) -> list[ToolRun]:
    """Queue bounded, corroborated username/email pivots for recursive person correlation."""

    queued = queue_automatic_username_pivots(db, investigation)
    queued.extend(queue_automatic_email_pivots(db, investigation))
    return queued


def find_email_pivot(
    investigation: Investigation, email: str, source_finding_id: str
) -> EmailPivotCandidate | None:
    target = normalise_email(email)
    if target is None:
        return None
    for candidate in discover_email_pivots(investigation):
        source_matches = (
            source_finding_id in candidate.finding_ids
            if candidate.finding_ids
            else not source_finding_id
        )
        if candidate.email.casefold() == target.casefold() and source_matches:
            return candidate
    return None


def find_username_pivot(
    investigation: Investigation, username: str, source_finding_id: str
) -> UsernamePivotCandidate | None:
    target = normalise_username(username)
    if target is None:
        return None
    for candidate in discover_username_pivots(investigation):
        source_matches = (
            source_finding_id in candidate.finding_ids
            if candidate.finding_ids
            else not source_finding_id
        )
        if candidate.username.casefold() == target.casefold() and source_matches:
            return candidate
    return None

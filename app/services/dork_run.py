"""Plan and track a paced, multi-engine dork run through the managed sock browser.

The run is deliberately small and slow: grouped ``site:`` queries keep the request count
low, queries are spread across engines so none sees a burst, and an engine that shows a
challenge is dropped for the rest of the run rather than retried. A challenge is never
solved automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Investigation
from app.models.schemas import QueryType
from app.services.case_identifiers import case_seeds
from app.services.managed_browser_extraction import uses_search_operators
from app.services.search_queries import generate_google_dorks

# Preference order, from live runs on 2026-11-12: DuckDuckGo and Brave honoured grouped
# site: searches on both browser engines. Bing returned the same ten links for three
# differently restricted queries, so it is opt-in and only receives unrestricted queries.
# Startpage showed a browser check that did not clear, and Google is the engine most likely
# to challenge; both are opt-in as well.
DORK_RUN_ENGINES = ("duckduckgo", "brave", "bing", "startpage", "google")
DEFAULT_ENGINES = ("duckduckgo", "brave")
GROUPED_SITE_ENGINES = frozenset({"duckduckgo", "brave", "startpage", "google"})
DORK_RUN_QUERY_TYPES = frozenset(
    {QueryType.USERNAME.value, QueryType.NAME.value, QueryType.EMAIL.value, QueryType.PHONE.value}
)
# Grouped social-platform searches suit a handle or a name; an email address or phone number
# rarely appears on a profile page, so those identifiers only get the wider pack.
SOCIAL_GROUP_QUERY_TYPES = frozenset({QueryType.USERNAME.value, QueryType.NAME.value})
DEFAULT_MAX_QUERIES = 10
MAX_QUERIES_LIMIT = 30
DEFAULT_DELAY_MIN = 45
DEFAULT_DELAY_MAX = 90
DELAY_BOUNDS = (10, 600)
ACTIVE_STATUSES = frozenset({"running"})

SOCIAL_SITE_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("social-meta", "Instagram, Threads and Facebook",
     ("instagram.com", "threads.net", "facebook.com")),
    ("social-video", "TikTok, Snapchat and YouTube",
     ("tiktok.com", "snapchat.com", "youtube.com")),
    ("social-discussion", "X, Reddit and LinkedIn",
     ("x.com", "twitter.com", "reddit.com", "linkedin.com")),
    ("social-community", "Pinterest, Twitch, Telegram and Tumblr",
     ("pinterest.com", "twitch.tv", "t.me", "tumblr.com")),
)
# Single-site dorks from the manual pack that the grouped social queries already cover.
_COVERED_DORK_IDS = frozenset(
    {
        "username-social-pack", "username-github", "username-reddit", "username-x",
        "username-instagram", "username-tiktok", "name-linkedin",
    }
)


@dataclass(frozen=True, slots=True)
class PlannedDork:
    id: str
    label: str
    query: str
    engine: str


def uses_operators(query: str) -> bool:
    """True when the query relies on a site: or filetype: restriction."""
    return uses_search_operators(query)


def state_key(investigation_id: str) -> str:
    return f"dork_run:{investigation_id}"


def _exact(value: str) -> str:
    cleaned = " ".join(value.replace('"', " ").split())
    return f'"{cleaned}"'


def normalise_engines(requested: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    chosen = {item.casefold() for item in requested}
    engines = tuple(engine for engine in DORK_RUN_ENGINES if engine in chosen)
    if not engines:
        raise ValueError("select at least one search engine for the automated run")
    return engines


def clamp_delays(delay_min: int, delay_max: int) -> tuple[int, int]:
    low, high = DELAY_BOUNDS
    delay_min = min(max(delay_min, low), high)
    delay_max = min(max(delay_max, delay_min), high)
    return delay_min, delay_max


def _seed_queries(value: str, query_type: QueryType) -> list[tuple[str, str, str]]:
    exact = _exact(value)
    queries: list[tuple[str, str, str]] = [
        (
            group_id,
            label,
            f"{exact} (" + " OR ".join(f"site:{site}" for site in sites) + ")",
        )
        for group_id, label, sites in SOCIAL_SITE_GROUPS
        if query_type.value in SOCIAL_GROUP_QUERY_TYPES
    ]
    for dork in generate_google_dorks(value, query_type):
        if dork.id in _COVERED_DORK_IDS or dork.reliability != "documented":
            continue
        queries.append((dork.id, dork.label, dork.query))
    return queries


def dork_seeds(investigation: Investigation) -> list[tuple[str, QueryType]]:
    """Usernames, full names, email addresses and phone numbers in the case.

    Covers the primary value and any identifiers added to the case.
    """
    seeds: list[tuple[str, QueryType]] = []
    for seed in case_seeds(investigation):
        value = seed.value.strip()
        if seed.query_type.value in SOCIAL_GROUP_QUERY_TYPES:
            value = value.lstrip("@")
        if seed.query_type.value in DORK_RUN_QUERY_TYPES and value:
            seeds.append((value, seed.query_type))
    return seeds


def plan_dork_run(
    investigation: Investigation,
    engines: tuple[str, ...],
    max_queries: int = DEFAULT_MAX_QUERIES,
) -> list[PlannedDork]:
    """Build the ordered query list: grouped social platforms first, then the wider pack.

    With several identifiers in the case the lists are interleaved, so every identifier gets
    its social-platform searches before any gets the wider pack.
    """
    seeds = dork_seeds(investigation)
    if not seeds:
        raise ValueError(
            "automated dork runs need a username, full name, email address or phone number "
            "in the investigation"
        )
    max_queries = min(max(max_queries, 1), MAX_QUERIES_LIMIT)
    per_seed = [(value, _seed_queries(value, query_type)) for value, query_type in seeds]
    queries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for position in range(max(len(items) for _, items in per_seed)):
        for seed_index, (value, items) in enumerate(per_seed):
            if position >= len(items):
                continue
            item_id, label, query = items[position]
            if query in seen:
                continue
            seen.add(query)
            if len(per_seed) > 1:
                item_id, label = f"{item_id}-{seed_index + 1}", f"{label} · {value}"
            queries.append((item_id, label, query))
    grouped_engines = tuple(engine for engine in engines if engine in GROUPED_SITE_ENGINES)
    if not grouped_engines:
        raise ValueError(
            "Bing ignores site and file-type restrictions; select DuckDuckGo, Brave, Startpage "
            "or Google as well"
        )
    plan: list[PlannedDork] = []
    grouped_count = plain_count = 0
    for item_id, label, query in queries[:max_queries]:
        if uses_operators(query):
            engine = grouped_engines[grouped_count % len(grouped_engines)]
            grouped_count += 1
        else:
            # Start the unrestricted queries from the end of the list so Bing takes its share.
            engine = engines[::-1][plain_count % len(engines)]
            plain_count += 1
        plan.append(PlannedDork(id=item_id, label=label, query=query, engine=engine))
    return plan


def new_state(
    plan: list[PlannedDork], engines: tuple[str, ...], delay_min: int, delay_max: int
) -> dict[str, Any]:
    return {
        "status": "running",
        "detail": None,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "engines": list(engines),
        "cooled_engines": [],
        "delay_min": delay_min,
        "delay_max": delay_max,
        "items": [
            {
                "id": item.id,
                "label": item.label,
                "query": item.query,
                "engine": item.engine,
                "status": "pending",
                "imported": 0,
                "extracted": 0,
                "browser_run_id": None,
                "detail": None,
            }
            for item in plan
        ],
    }


def load_state(db: Session, investigation_id: str) -> dict[str, Any] | None:
    # Sessions keep loaded rows after commit, so force a fresh read: a cancel written by
    # another request must be visible to the background queue.
    setting = db.get(ApplicationSetting, state_key(investigation_id), populate_existing=True)
    if setting is None or not isinstance(setting.value, dict) or not setting.value.get("items"):
        return None
    return {**setting.value, "items": [dict(item) for item in setting.value["items"]]}


def save_state(db: Session, investigation_id: str, state: dict[str, Any]) -> None:
    setting = db.get(ApplicationSetting, state_key(investigation_id))
    if setting is None:
        setting = ApplicationSetting(key=state_key(investigation_id), value={})
        db.add(setting)
    # Assign a fresh dict so SQLAlchemy sees the JSON column change.
    setting.value = {**state, "items": [dict(item) for item in state["items"]]}
    setting.updated_at = datetime.now(UTC)


def finish_state(state: dict[str, Any], status: str, detail: str | None = None) -> None:
    state["status"] = status
    state["detail"] = detail
    state["finished_at"] = datetime.now(UTC).isoformat()
    for item in state["items"]:
        if item["status"] in {"pending", "running"}:
            item["status"] = "skipped"
            item["detail"] = detail


def available_engine(
    state: dict[str, Any], preferred: str, query: str = "", *, avoid: str | None = None
) -> str | None:
    """Return the preferred engine, or the next usable one that has not been challenged.

    Queries with site: or filetype: restrictions only go to engines that honour them. ``avoid`` asks for a
    different engine than the one that just failed, when another is available.
    """
    cooled = set(state["cooled_engines"])
    engines = [engine for engine in state["engines"] if engine not in cooled]
    if uses_operators(query):
        engines = [engine for engine in engines if engine in GROUPED_SITE_ENGINES]
    if avoid is not None and any(engine != avoid for engine in engines):
        engines = [engine for engine in engines if engine != avoid]
        preferred = engines[0]
    if not engines:
        return None
    return preferred if preferred in engines else engines[0]


def summarise(state: dict[str, Any]) -> dict[str, int]:
    counts = {"total": len(state["items"]), "imported": 0}
    for item in state["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        counts["imported"] += int(item.get("imported") or 0)
    return counts

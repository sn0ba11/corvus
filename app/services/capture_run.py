"""Plan and track a paced run that captures found profile pages through the sock browser.

Collectors report that an account exists; the richest public detail (bio, counts, links,
avatar) is on the profile page itself, and for some platforms only a signed-in browser can
read it. This run opens each found profile page in the managed browser, one at a time and
slowly, so the analyst does not have to submit every address by hand.

It never solves a challenge or signs in. A platform that shows a login wall or challenge is
dropped for the rest of the run and its remaining pages are left for manual handling.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.db import ApplicationSetting, Finding, Investigation
from app.services.case_identifiers import case_seeds
from app.services.managed_browser_extraction import policy_for_url
from app.services.profile_links import profile_account_from_url

DEFAULT_MAX_PAGES = 10
MAX_PAGES_LIMIT = 30
DEFAULT_DELAY_MIN = 30
DEFAULT_DELAY_MAX = 75
DELAY_BOUNDS = (10, 600)
# A platform that twice in a row returns a page with no readable profile is very likely
# showing a sign-in wall the browser did not flag; stop sending it pages.
NO_PROFILE_LIMIT = 2


def state_key(investigation_id: str) -> str:
    return f"capture_run:{investigation_id}"


def clamp_delays(delay_min: int, delay_max: int) -> tuple[int, int]:
    low, high = DELAY_BOUNDS
    delay_min = min(max(delay_min, low), high)
    delay_max = min(max(delay_max, delay_min), high)
    return delay_min, delay_max


def _profile_already_read(finding: Finding) -> bool:
    """True once a browser capture has read a profile into this finding.

    A capture that loaded the page but read nothing (a sign-in wall, say) does not count, so
    the page stays in the list for another attempt after the analyst has signed in.
    """
    if finding.extra.get("managed_browser"):
        return True
    sources = finding.extra.get("source_metadata")
    return isinstance(sources, dict) and any(
        isinstance(value, dict) and value.get("managed_browser") for value in sources.values()
    )


def capture_candidates(investigation: Investigation) -> list[dict[str, str]]:
    """Found profile pages the sock browser can read and has not captured yet.

    Only an account's own page counts: a post, video or listing is not a profile. A search
    result is included only when its handle is one of the usernames in the case.
    """
    case_handles = {
        seed.value.strip().lstrip("@").casefold()
        for seed in case_seeds(investigation)
        if seed.query_type.value == "username"
    }
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []
    for finding in investigation.findings:
        url = finding.canonical_url
        if not url or finding.status not in {"found", "likely", "unknown"}:
            continue
        if finding.status == "unknown" and finding.finding_type != "unverified_profile_lead":
            continue
        policy = policy_for_url(url)
        if policy is None or policy.kind != "social_profile":
            continue
        account = profile_account_from_url(url)
        if account is None:
            continue
        if (finding.finding_type == "search_result" or finding.status == "unknown") and account[1].casefold() not in case_handles:
            # A search hit for some other handle is a lead, not this subject's account.
            continue
        key = url.rstrip("/").casefold()
        if key in seen or _profile_already_read(finding):
            continue
        seen.add(key)
        candidates.append({"url": url, "platform": policy.platform, "handle": account[1]})
    candidates.sort(key=lambda item: (item["platform"], item["handle"].casefold()))
    return candidates


def plan_capture_run(
    investigation: Investigation, max_pages: int = DEFAULT_MAX_PAGES
) -> list[dict[str, str]]:
    """Order the pages so consecutive requests go to different platforms where possible."""
    candidates = capture_candidates(investigation)
    if not candidates:
        raise ValueError("no found profile pages are waiting to be captured")
    max_pages = min(max(max_pages, 1), MAX_PAGES_LIMIT)
    by_platform: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in candidates:
        by_platform[candidate["platform"]].append(candidate)
    plan: list[dict[str, str]] = []
    position = 0
    while len(plan) < len(candidates):
        for platform in sorted(by_platform):
            if position < len(by_platform[platform]):
                plan.append(by_platform[platform][position])
        position += 1
    return plan[:max_pages]


def new_state(plan: list[dict[str, str]], delay_min: int, delay_max: int) -> dict[str, Any]:
    return {
        "status": "running",
        "detail": None,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "cooled_platforms": [],
        "delay_min": delay_min,
        "delay_max": delay_max,
        "items": [
            {
                **item,
                "status": "pending",
                "imported": 0,
                "browser_run_id": None,
                "detail": None,
            }
            for item in plan
        ],
    }


def load_state(db: Session, investigation_id: str) -> dict[str, Any] | None:
    # Force a fresh read: a cancel written by another request must reach the background queue.
    setting = db.get(ApplicationSetting, state_key(investigation_id), populate_existing=True)
    if setting is None or not isinstance(setting.value, dict) or not setting.value.get("items"):
        return None
    return {**setting.value, "items": [dict(item) for item in setting.value["items"]]}


def save_state(db: Session, investigation_id: str, state: dict[str, Any]) -> None:
    setting = db.get(ApplicationSetting, state_key(investigation_id))
    if setting is None:
        setting = ApplicationSetting(key=state_key(investigation_id), value={})
        db.add(setting)
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


def cool_platform(state: dict[str, Any], platform: str, reason: str) -> None:
    """Stop sending pages to a platform that asked for a sign-in or showed a challenge."""
    if platform not in state["cooled_platforms"]:
        state["cooled_platforms"] = [*state["cooled_platforms"], platform]
    for item in state["items"]:
        if item["platform"] == platform and item["status"] == "pending":
            item["status"] = "skipped"
            item["detail"] = reason


def platform_keeps_returning_nothing(state: dict[str, Any], platform: str) -> bool:
    attempted = [
        item["status"]
        for item in state["items"]
        if item["platform"] == platform and item["status"] in {"completed", "no_profile"}
    ]
    return attempted[-NO_PROFILE_LIMIT:] == ["no_profile"] * NO_PROFILE_LIMIT


def summarise(state: dict[str, Any]) -> dict[str, int]:
    counts = {"total": len(state["items"]), "imported": 0}
    for item in state["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        counts["imported"] += int(item.get("imported") or 0)
    return counts

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.models.db import Investigation

POSITIVE_STATUSES = {"found", "likely"}
ACTIVE_STATUSES = {"queued", "running"}
FAILED_RUN_STATUSES = {"failed", "timed_out", "cancelled"}
# Verification outcomes that mean Corvus tried and failed to independently confirm a
# claim, as distinct from never having checked it at all. A finding in one of these
# states must not count as a positive result no matter what the source collector
# itself asserted.
UNRESOLVED_VERIFICATION_STATES = {
    "access_blocked",
    "rate_limited",
    "identifier_mismatch",
    "generic_response",
    "retrieval_error",
}


def _has_unresolved_verification(finding: Any) -> bool:
    verification = finding.extra.get("verification")
    return isinstance(verification, dict) and verification.get("state") in UNRESOLVED_VERIFICATION_STATES


def investigation_summary(investigation: Investigation) -> dict[str, Any]:
    findings = list(investigation.findings)
    runs = list(investigation.runs)
    locations = list(investigation.location_observations)
    browser_runs = list(investigation.browser_runs)
    finding_statuses = Counter(finding.status for finding in findings)
    run_statuses = Counter(run.status for run in runs)
    browser_run_statuses = Counter(run.status for run in browser_runs)
    platforms = Counter((finding.platform or "Uncategorised") for finding in findings)

    positive = [
        finding
        for finding in findings
        if finding.status in POSITIVE_STATUSES and not _has_unresolved_verification(finding)
    ]
    rich_profiles = [
        finding
        for finding in positive
        if isinstance(finding.extra.get("profile"), dict)
        and any(finding.extra.get("profile", {}).values())
    ]
    cached_images = [
        finding
        for finding in findings
        if isinstance(finding.extra.get("image_cache"), dict)
        and finding.extra.get("image_cache", {}).get("sha256")
    ]
    rate_limited_runs = [run for run in runs if run.rate_limited]
    failed_runs = [run for run in runs if run.status in FAILED_RUN_STATUSES]
    search_imports = [finding for finding in findings if "search_import" in finding.source_tools]
    profile_claims = [
        finding
        for finding in findings
        if finding.canonical_url
        and (
            finding.finding_type
            in {
                "profile",
                "instagram_profile",
                "unverified_profile_lead",
                "dead_profile",
            }
            or any(
                token in finding.finding_type.casefold()
                for token in ("profile", "account", "social")
            )
        )
    ]
    verification_states = Counter(
        str(finding.extra.get("verification", {}).get("state"))
        for finding in profile_claims
        if isinstance(finding.extra.get("verification"), dict)
        and finding.extra.get("verification", {}).get("state")
    )
    verification_attempt_count = sum(verification_states.values())
    unresolved_verification_count = sum(
        verification_states[state] for state in UNRESOLVED_VERIFICATION_STATES
    )
    wayback_checked = [
        finding
        for finding in findings
        if isinstance(finding.extra.get("wayback"), dict)
        and finding.extra.get("wayback", {}).get("automatic_checked_at")
    ]
    wayback_archived = [
        finding
        for finding in wayback_checked
        if int(finding.extra.get("wayback", {}).get("capture_count", 0)) > 0
    ]
    wayback_capture_count = sum(
        int(finding.extra.get("wayback", {}).get("capture_count", 0)) for finding in wayback_checked
    )

    account_matches = 0
    accepted_account_matches = 0
    image_matches = 0
    accepted_image_matches = 0
    for finding in findings:
        account_correlation = finding.extra.get("account_correlation", {})
        if isinstance(account_correlation, dict):
            matches = account_correlation.get("matches", [])
            if isinstance(matches, list):
                account_matches += len(matches)
                accepted_account_matches += sum(
                    1
                    for match in matches
                    if isinstance(match, dict) and match.get("decision") == "accepted"
                )
        image_correlation = finding.extra.get("image_correlation", {})
        if isinstance(image_correlation, dict):
            matches = image_correlation.get("matches", [])
            if isinstance(matches, list):
                image_matches += len(matches)
                accepted_image_matches += sum(
                    1
                    for match in matches
                    if isinstance(match, dict) and match.get("decision") == "accepted"
                )

    platform_summary = [
        {
            "platform": platform,
            "count": count,
            "positive_count": sum(
                1 for finding in positive if (finding.platform or "Uncategorised") == platform
            ),
        }
        for platform, count in sorted(
            platforms.items(), key=lambda item: (-item[1], item[0].casefold())
        )
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "findings_total": len(findings),
        "positive_findings": len(positive),
        "found_findings": finding_statuses["found"],
        "likely_findings": finding_statuses["likely"],
        "unknown_findings": finding_statuses["unknown"],
        "not_found_findings": finding_statuses["not_found"],
        "error_findings": finding_statuses["error"],
        "rate_limited_findings": finding_statuses["rate_limited"],
        "platform_count": len(platforms),
        "platform_summary": platform_summary,
        "rich_profile_count": len(rich_profiles),
        "cached_image_count": len(cached_images),
        "search_import_count": len(search_imports),
        "profile_claim_count": len(profile_claims),
        "verification_attempt_count": verification_attempt_count,
        "unvalidated_profile_count": max(0, len(profile_claims) - verification_attempt_count),
        "verified_account_count": verification_states["confirmed_account"]
        + verification_states["corroborated_account"],
        "likely_account_count": verification_states["unverified_candidate"],
        "dead_profile_count": verification_states["dead_profile"],
        "unresolved_verification_count": unresolved_verification_count,
        "blocked_verification_count": verification_states["access_blocked"]
        + verification_states["rate_limited"],
        "verification_states": dict(verification_states),
        "wayback_checked_count": len(wayback_checked),
        "wayback_archived_count": len(wayback_archived),
        "wayback_capture_count": wayback_capture_count,
        "location_observation_count": len(locations),
        "location_place_count": len(
            {
                (
                    location.place_name.casefold(),
                    (location.locality or "").casefold(),
                    (location.region or "").casefold(),
                    (location.country or "").casefold(),
                )
                for location in locations
            }
        ),
        "tool_run_count": len(runs),
        "completed_run_count": run_statuses["completed"],
        "active_run_count": sum(run_statuses[status] for status in ACTIVE_STATUSES),
        "failed_run_count": len(failed_runs),
        "rate_limited_run_count": len(rate_limited_runs),
        "browser_run_count": len(browser_runs),
        "browser_completed_count": browser_run_statuses["completed"],
        "browser_manual_action_count": browser_run_statuses["manual_action_required"],
        "browser_failed_count": browser_run_statuses["failed"],
        "account_match_count": account_matches,
        "accepted_account_match_count": accepted_account_matches,
        "image_match_count": image_matches,
        "accepted_image_match_count": accepted_image_matches,
    }


def dashboard_summary(investigations: list[Investigation]) -> dict[str, int]:
    summaries = [investigation_summary(item) for item in investigations]
    return {
        "investigation_count": len(investigations),
        "active_investigations": sum(
            1 for item in investigations if item.status in ACTIVE_STATUSES
        ),
        "completed_investigations": sum(1 for item in investigations if item.status == "completed"),
        "positive_findings": sum(item["positive_findings"] for item in summaries),
        "location_observations": sum(item["location_observation_count"] for item in summaries),
        "failed_runs": sum(item["failed_run_count"] for item in summaries),
    }

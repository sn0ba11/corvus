import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import get_settings
from app.models.db import Investigation
from app.services.investigation_accounts import build_investigation_account_view
from app.services.location_intelligence import build_location_clusters
from app.services.platform_icons import platform_icon_svg
from app.services.presentation import investigation_summary


def _avatar_evidence(extra: dict[str, Any]) -> dict[str, Any]:
    profile = extra.get("profile", {})
    cache = extra.get("image_cache", {})
    avatar_url = profile.get("avatar_url") if isinstance(profile, dict) else None
    cache = cache if isinstance(cache, dict) else {}
    error = cache.get("error")
    if not avatar_url:
        state = "unavailable"
    elif error:
        blocked_markers = (
            "blocked",
            "private",
            "non-routable",
            "redirect",
            "only http",
            "invalid image url",
        )
        state = (
            "blocked"
            if any(marker in str(error).lower() for marker in blocked_markers)
            else "unavailable"
        )
    elif cache.get("sha256"):
        state = "cached"
    else:
        state = "not_retrieved"
    return {
        "state": state,
        "source_url": avatar_url,
        "retrieved_at": cache.get("retrieved_at"),
        "mime_type": cache.get("mime_type"),
        "byte_size": cache.get("size_bytes"),
        "sha256": cache.get("sha256"),
        "error": error,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def investigation_dict(investigation: Investigation) -> dict[str, Any]:
    settings = get_settings()
    location_observations = list(investigation.location_observations)
    location_clusters = build_location_clusters(location_observations)
    summary = investigation_summary(investigation)
    account_view = build_investigation_account_view(investigation)
    summary.update(
        {
            "unified_account_count": account_view["summary"]["account_count"],
            "priority_account_count": account_view["summary"]["priority_account_count"],
            "technical_account_count": account_view["summary"]["technical_account_count"],
            "case_timeline_event_count": account_view["summary"]["timeline_event_count"],
            "account_timeline_event_count": account_view["summary"]["account_timeline_event_count"],
        }
    )
    return {
        "schema_version": "1.3",
        "application": {
            "name": settings.app_name,
            "version": settings.version,
            "build_commit": settings.build_commit or "development",
            "brand_mark": "OW",
        },
        "investigation": {
            "id": investigation.id,
            "title": investigation.title,
            "query": investigation.query,
            "query_type": investigation.query_type,
            "case_reference": investigation.case_reference,
            "created_at": investigation.created_at.astimezone(UTC).isoformat(),
            "status": investigation.status,
            "duration_ms": investigation.duration_ms,
            "tags": investigation.tags,
            "notes": investigation.notes,
            "selected_tools": investigation.selected_tools,
            "auto_follow_username_pivots": investigation.auto_follow_username_pivots,
        },
        "summary": summary,
        "account_view": account_view,
        "browser_runs": [
            {
                "id": run.id,
                "collector_id": run.collector_id,
                "session_name": run.session_name,
                "requested_url": run.requested_url,
                "final_url": run.final_url,
                "status": run.status,
                "page_title": run.page_title,
                "challenge_type": run.challenge_type,
                "browser_version": run.browser_version,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "observed_at": _iso(run.observed_at),
                "error_code": run.error_code,
                "error_message": run.error_message,
                "evidence": {
                    "screenshot_reference": run.screenshot_reference,
                    "screenshot_sha256": run.screenshot_sha256,
                    "html_reference": run.html_reference,
                    "html_sha256": run.html_sha256,
                    "manifest_reference": run.manifest_reference,
                    "manifest_sha256": run.manifest_sha256,
                },
            }
            for run in investigation.browser_runs
        ],
        "tool_runs": [
            {
                "id": r.id,
                "tool_id": r.tool_id,
                "display_name": r.display_name,
                "status": r.status,
                "version": r.tool_version,
                "commit": r.tool_commit,
                "timeout_seconds": r.timeout_seconds,
                "query_value": r.query_value or investigation.query,
                "query_type": r.query_type or investigation.query_type,
                "pivot_depth": r.pivot_depth,
                "pivot_source_finding_id": r.pivot_source_finding_id,
                "pivot_automatic": r.pivot_automatic,
                "duration_ms": r.duration_ms,
                "started_at": r.started_at.astimezone(UTC).isoformat() if r.started_at else None,
                "finished_at": r.finished_at.astimezone(UTC).isoformat() if r.finished_at else None,
                "rate_limited": r.rate_limited,
                "error_code": r.error_code,
                "error_message": r.error_message,
                "raw_output_reference": r.raw_output_reference,
            }
            for r in investigation.runs
        ],
        "location_summary": [
            {
                "place_name": cluster.place_name,
                "locality": cluster.locality,
                "region": cluster.region,
                "country": cluster.country,
                "classification": cluster.classification,
                "observation_count": cluster.observation_count,
                "evidence_types": cluster.evidence_types,
                "source_platforms": cluster.source_platforms,
                "first_observed_at": _iso(cluster.first_observed_at),
                "last_observed_at": _iso(cluster.last_observed_at),
                "earliest_occurred_at": _iso(cluster.earliest_occurred_at),
                "latest_occurred_at": _iso(cluster.latest_occurred_at),
                "maximum_confidence": cluster.maximum_confidence,
                "privacy_masked_count": cluster.privacy_masked_count,
            }
            for cluster in location_clusters
        ],
        "location_observations": [
            {
                "id": item.id,
                "finding_id": item.finding_id,
                "place_name": item.place_name,
                "locality": item.locality,
                "region": item.region,
                "country": item.country,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "precision_radius_m": item.precision_radius_m,
                "evidence_type": item.evidence_type,
                "occurred_at": _iso(item.occurred_at),
                "published_at": _iso(item.published_at),
                "first_observed_at": _iso(item.first_observed_at),
                "last_observed_at": _iso(item.last_observed_at),
                "source_platform": item.source_platform,
                "source_url": item.source_url,
                "account_identifier": item.account_identifier,
                "privacy_masked": item.privacy_masked,
                "confidence": item.confidence,
                "raw_evidence": item.raw_evidence,
            }
            for item in location_observations
        ],
        "findings": [
            {
                "id": f.id,
                "query_type": f.query_type,
                "queried_value": f.queried_value,
                "finding_type": f.finding_type,
                "platform": f.platform,
                "value": f.value,
                "canonical_url": f.canonical_url,
                "display_url": f.display_url,
                "status": f.status,
                "confidence": f.confidence,
                "title": f.title,
                "description": f.description,
                "profile": f.extra.get("profile", {}),
                "platform_details": f.extra.get("platform_details", []),
                "avatar_evidence": _avatar_evidence(f.extra),
                "metadata": f.extra,
                "source_tool": f.source_tool,
                "source_tools": f.source_tools,
                "source_record_id": f.source_record_id,
                "observed_at": f.observed_at.astimezone(UTC).isoformat(),
                "raw_output_reference": f.raw_output_reference,
            }
            for f in investigation.findings
        ],
        "limitations": [
            "A found username is only a possible association, not proof of identity.",
            "Not-found and unknown results can reflect blocking, site changes, or transient errors.",
            "Confidence is omitted unless an upstream supplies a meaningful calibrated value.",
            "Location observations do not by themselves prove residence or physical presence.",
            "A Wayback first-archive date is the earliest public indexed capture found, not proof of page creation or publication.",
            "Browser captures preserve visible source evidence, but authenticated access does not independently verify identity or authorship.",
        ],
    }


def json_export(investigation: Investigation) -> bytes:
    return json.dumps(investigation_dict(investigation), indent=2, ensure_ascii=False).encode()


def _spreadsheet_safe(value: object) -> object:
    """Neutralise untrusted values that spreadsheet software may execute as formulas."""

    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{value}"
    return value


def csv_export(investigation: Investigation) -> bytes:
    output = io.StringIO(newline="")
    fields = [
        "id",
        "platform",
        "finding_type",
        "value",
        "status",
        "confidence",
        "canonical_url",
        "display_name",
        "avatar_url",
        "location",
        "account_id",
        "verified",
        "followers",
        "following",
        "source_tools",
        "observed_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for finding in investigation.findings:
        row = {
            "id": finding.id,
            "platform": finding.platform,
            "finding_type": finding.finding_type,
            "value": finding.value,
            "status": finding.status,
            "confidence": finding.confidence,
            "canonical_url": finding.canonical_url,
            "display_name": finding.extra.get("profile", {}).get("display_name"),
            "avatar_url": finding.extra.get("profile", {}).get("avatar_url"),
            "location": finding.extra.get("profile", {}).get("location"),
            "account_id": finding.extra.get("profile", {}).get("account_id"),
            "verified": finding.extra.get("profile", {}).get("verified"),
            "followers": finding.extra.get("profile", {}).get("followers"),
            "following": finding.extra.get("profile", {}).get("following"),
            "source_tools": ";".join(finding.source_tools),
            "observed_at": finding.observed_at.astimezone(UTC).isoformat(),
        }
        writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def render_html_report(investigation: Investigation, *, interactive: bool = False) -> bytes:
    templates = Path(__file__).resolve().parents[1] / "templates"
    env = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape(["html", "xml"])
    )
    env.globals["platform_icon_svg"] = platform_icon_svg
    return (
        env.get_template("report.html")
        .render(report=investigation_dict(investigation), interactive=interactive)
        .encode()
    )


def html_export(investigation: Investigation) -> bytes:
    return render_html_report(investigation)

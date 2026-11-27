from __future__ import annotations

import hashlib
import ipaddress
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.models.db import Finding, Investigation
from app.services.account_correlation import extract_domains
from app.services.discord_intelligence import normalise_discord_reference
from app.services.investigation_accounts import build_investigation_account_view
from app.services.pivots import discover_username_pivots
from app.services.profile_links import extract_profile_link_candidates
from app.services.telegram_intelligence import normalise_telegram_reference

GraphNode = dict[str, Any]
GraphEdge = dict[str, Any]
GraphPosition = dict[str, float]

GRAPH_LAYOUT_SETTING_PREFIX = "investigation_graph_layout:"
POSITIVE_STATUSES = {"found", "likely"}
CONFIRMED_VERIFICATION_STATES = {"confirmed_account", "corroborated_account"}
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}(?![\w.-])", re.IGNORECASE
)


def graph_layout_setting_key(investigation_id: str) -> str:
    return f"{GRAPH_LAYOUT_SETTING_PREFIX}{investigation_id}"


def _stable_token(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()[:20]


def _clean_text(value: object, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_external_url(value: object) -> str | None:
    url = _clean_text(value, limit=2048)
    if not url:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = parsed.hostname.rstrip(".").casefold()
    local_suffixes = (".localhost", ".local", ".lan", ".internal", ".home", ".home.arpa")
    if host == "localhost" or "." not in host or host.endswith(local_suffixes):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return url


def _normalised_host(url: str | None) -> str | None:
    safe_url = _safe_external_url(url)
    if not safe_url:
        return None
    parsed = urlsplit(safe_url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return host or None


def _profile_metadata(finding: Finding) -> dict[str, Any]:
    profile = finding.extra.get("profile", {})
    return profile if isinstance(profile, dict) else {}


def _email_addresses_from_finding(finding: Finding) -> list[tuple[str, str]]:
    candidates: list[tuple[object, str]] = [
        (finding.value, "finding value"),
        (finding.title, "finding title"),
        (finding.description, "finding description"),
    ]
    profile = _profile_metadata(finding)
    candidates.extend(
        [
            (profile.get("biography"), "profile biography"),
            (finding.extra.get("public_email"), "public email"),
            (finding.extra.get("email"), "email field"),
        ]
    )
    platform_details = finding.extra.get("platform_details", [])
    if isinstance(platform_details, list):
        for detail in platform_details[:50]:
            if not isinstance(detail, dict):
                continue
            label = _clean_text(detail.get("label"), limit=80).casefold()
            if "email" in label or "mail" in label or "contact" in label:
                candidates.append((detail.get("value"), label or "platform detail"))

    found: dict[str, tuple[str, str]] = {}
    for value, source in candidates:
        if not isinstance(value, str):
            continue
        for match in EMAIL_RE.findall(value):
            address = match.strip().strip(".,;:()[]{}<>")
            if address:
                found.setdefault(address.casefold(), (address, source))
    return list(found.values())


def _verification_state(finding: Finding) -> str:
    verification = finding.extra.get("verification", {})
    if not isinstance(verification, dict):
        return ""
    return _clean_text(verification.get("state"), limit=80)


def _node_state(finding: Finding) -> str:
    verification = _verification_state(finding)
    if verification in CONFIRMED_VERIFICATION_STATES:
        return "confirmed"
    if verification in {"dead_profile", "identifier_mismatch"}:
        return "contradicted"
    if finding.status in POSITIVE_STATUSES:
        return "observed"
    if finding.status in {"not_found", "error"}:
        return "contradicted"
    return "candidate"


def _finding_node_type(finding: Finding) -> str:
    kind = finding.finding_type.casefold()
    if any(token in kind for token in ("profile", "account", "social")):
        return "profile"
    if "email" in kind:
        return "email"
    if kind == "tracking_identifier":
        return "identifier"
    if "domain" in kind or "hostname" in kind:
        return "domain"
    if any(token in kind for token in ("document", "file", "pdf")):
        return "document"
    if "image" in kind or "avatar" in kind:
        return "image"
    return "finding"


def _finding_hidden_by_default(finding: Finding) -> bool:
    if _finding_node_type(finding) == "email" or finding.finding_type == "tracking_identifier":
        return False
    state = _node_state(finding)
    source_count = len(set(finding.source_tools or [finding.source_tool]))
    profile = _profile_metadata(finding)
    rich_profile = any(
        profile.get(key) for key in ("display_name", "account_id", "biography", "location")
    )
    confidence = float(finding.confidence or 0.0)
    return not (
        state == "confirmed"
        or source_count >= 2
        or (finding.status in POSITIVE_STATUSES and rich_profile and confidence >= 0.85)
    )


def _identifier_type(query_type: str) -> str:
    return {
        "name": "subject",
        "username": "username",
        "email": "email",
        "domain": "domain",
    }.get(query_type, "identifier")


def _finding_label(finding: Finding) -> str:
    profile = _profile_metadata(finding)
    return (
        _clean_text(profile.get("display_name"))
        or _clean_text(finding.title)
        or _clean_text(finding.value)
        or _clean_text(finding.platform)
        or finding.finding_type.replace("_", " ").title()
    )


def _finding_links(investigation: Investigation, finding: Finding) -> list[dict[str, str]]:
    links = [
        {
            "label": "View parsed record",
            "url": f"/investigations/{investigation.id}#finding-{finding.id}",
            "kind": "internal",
        },
        {
            "label": "Open raw evidence",
            "url": f"/investigations/{investigation.id}/raw-json?run_id={finding.tool_run_id}",
            "kind": "internal",
        },
    ]
    public_url = _safe_external_url(finding.canonical_url)
    if public_url and finding.status in POSITIVE_STATUSES:
        links.append({"label": "Open public source", "url": public_url, "kind": "external"})
    profile = _profile_metadata(finding)
    if profile.get("avatar_url"):
        links.append(
            {
                "label": "Open retained profile image",
                "url": f"/findings/{finding.id}/avatar",
                "kind": "internal",
            }
        )
    return links


def _correlation_state(decision: object) -> str:
    value = str(decision or "unreviewed")
    if value == "accepted":
        return "confirmed"
    if value == "rejected":
        return "rejected"
    return "candidate"


def _valid_saved_position(value: object) -> GraphPosition | None:
    if not isinstance(value, dict):
        return None
    x = value.get("x")
    y = value.get("y")
    if not isinstance(x, int | float) or not isinstance(y, int | float):
        return None
    if not (-100000 <= float(x) <= 100000 and -100000 <= float(y) <= 100000):
        return None
    return {"x": round(float(x), 2), "y": round(float(y), 2)}


def build_investigation_graph(
    investigation: Investigation,
    *,
    saved_positions: dict[str, object] | None = None,
) -> dict[str, Any]:
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    positions = saved_positions or {}

    def add_node(
        node_id: str,
        *,
        node_type: str,
        label: str,
        subtitle: str = "",
        state: str = "observed",
        confidence: float | None = None,
        details: list[dict[str, str]] | None = None,
        links: list[dict[str, str]] | None = None,
        avatar_url: str | None = None,
        platform: str | None = None,
        occurred_at: str | None = None,
        prominent: bool = False,
        hidden_by_default: bool = False,
    ) -> None:
        if node_id in nodes:
            if not hidden_by_default:
                nodes[node_id]["data"]["hidden_by_default"] = False
            if prominent:
                nodes[node_id]["data"]["prominent"] = True
            return
        node: GraphNode = {
            "data": {
                "id": node_id,
                "type": node_type,
                "label": label,
                "subtitle": subtitle,
                "state": state,
                "confidence": confidence,
                "details": details or [],
                "links": links or [],
                "avatar_url": avatar_url,
                "platform": platform,
                "occurred_at": occurred_at,
                "prominent": prominent,
                "hidden_by_default": hidden_by_default,
            }
        }
        saved = _valid_saved_position(positions.get(node_id))
        if saved:
            node["position"] = saved
        nodes[node_id] = node

    def add_edge(
        source: str,
        target: str,
        *,
        relation: str,
        label: str,
        state: str = "observed",
        confidence: float | None = None,
        support_count: int = 1,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        if source not in nodes or target not in nodes or source == target:
            return
        edge_id = f"edge:{_stable_token(source, target, relation, label)}"
        if edge_id in edges:
            edges[edge_id]["data"]["support_count"] = max(
                int(edges[edge_id]["data"].get("support_count", 1)), support_count
            )
            return
        edges[edge_id] = {
            "data": {
                "id": edge_id,
                "source": source,
                "target": target,
                "relation": relation,
                "label": label,
                "state": state,
                "confidence": confidence,
                "support_count": support_count,
                "details": details or [],
            }
        }

    subject_id = "subject"
    add_node(
        subject_id,
        node_type="subject",
        label=investigation.title,
        subtitle=f"{investigation.query_type.title()} investigation",
        state="confirmed",
        details=[
            {"label": "Search value", "value": investigation.query},
            {"label": "Created", "value": investigation.created_at.isoformat()},
            {"label": "Status", "value": investigation.status},
        ],
        links=[
            {
                "label": "Evidence register",
                "url": f"/investigations/{investigation.id}#findings-section",
                "kind": "internal",
            },
            {
                "label": "Search workbench",
                "url": f"/investigations/{investigation.id}/search-queries",
                "kind": "internal",
            },
            {
                "label": "Location intelligence",
                "url": f"/investigations/{investigation.id}/locations",
                "kind": "internal",
            },
            {
                "label": "Wayback",
                "url": f"/investigations/{investigation.id}/wayback",
                "kind": "internal",
            },
        ],
    )

    primary_identifier_id = (
        f"identifier:{_stable_token(investigation.query_type, investigation.query.casefold())}"
    )
    add_node(
        primary_identifier_id,
        node_type=_identifier_type(investigation.query_type),
        label=investigation.query,
        subtitle="Original search value",
        state="confirmed",
        details=[{"label": "Query type", "value": investigation.query_type}],
        prominent=investigation.query_type == "email",
    )
    add_edge(
        subject_id,
        primary_identifier_id,
        relation="searched_as",
        label="searched as",
        state="confirmed",
    )

    finding_ids = {finding.id for finding in investigation.findings}
    finding_lookup = {finding.id: finding for finding in investigation.findings}
    telegram_message_nodes: dict[tuple[str, str], str] = {}
    for telegram_finding in investigation.findings:
        if telegram_finding.finding_type != "telegram_message":
            continue
        export_sha = _clean_text(telegram_finding.extra.get("telegram_export_sha256"), limit=64)
        message_id = _clean_text(telegram_finding.extra.get("telegram_message_id"), limit=80)
        if export_sha and message_id:
            telegram_message_nodes[(export_sha, message_id)] = f"finding:{telegram_finding.id}"
    discord_message_nodes: dict[tuple[str, str], str] = {}
    for discord_finding in investigation.findings:
        if discord_finding.finding_type != "discord_message":
            continue
        export_sha = _clean_text(discord_finding.extra.get("discord_export_sha256"), limit=64)
        message_id = _clean_text(discord_finding.extra.get("discord_message_id"), limit=32)
        if export_sha and message_id:
            discord_message_nodes[(export_sha, message_id)] = f"finding:{discord_finding.id}"
    timeline_event_count = 0
    for finding in sorted(investigation.findings, key=lambda item: (item.observed_at, item.id)):
        node_id = f"finding:{finding.id}"
        profile = _profile_metadata(finding)
        verification = _verification_state(finding)
        image_cache = finding.extra.get("image_cache", {})
        image_sha = image_cache.get("sha256") if isinstance(image_cache, dict) else None
        image_sha = image_sha or finding.extra.get("avatar_sha256")
        retained_avatar = bool(image_sha or finding.extra.get("avatar_local_path"))
        finding_hidden = _finding_hidden_by_default(finding)
        details = [
            {"label": "Finding type", "value": finding.finding_type.replace("_", " ")},
            {"label": "Platform", "value": finding.platform or "Uncategorised"},
            {"label": "Status", "value": finding.status},
            {"label": "Observed", "value": finding.observed_at.isoformat()},
            {"label": "Sources", "value": ", ".join(finding.source_tools or [finding.source_tool])},
        ]
        if verification:
            details.append({"label": "Verification", "value": verification.replace("_", " ")})
        if profile.get("account_id"):
            details.append({"label": "Account ID", "value": _clean_text(profile.get("account_id"))})
        if profile.get("biography"):
            details.append(
                {"label": "Biography", "value": _clean_text(profile.get("biography"), limit=500)}
            )
        add_node(
            node_id,
            node_type=_finding_node_type(finding),
            label=_finding_label(finding),
            subtitle=finding.platform or finding.finding_type.replace("_", " ").title(),
            state=_node_state(finding),
            confidence=finding.confidence,
            details=details,
            links=_finding_links(investigation, finding),
            avatar_url=(f"/findings/{finding.id}/avatar" if retained_avatar else None),
            platform=finding.platform,
            hidden_by_default=finding_hidden,
        )

        queried_value = finding.queried_value or investigation.query
        queried_type = finding.query_type or investigation.query_type
        identifier_id = f"identifier:{_stable_token(queried_type, queried_value.casefold())}"
        if identifier_id not in nodes:
            add_node(
                identifier_id,
                node_type=_identifier_type(queried_type),
                label=queried_value,
                subtitle=f"{queried_type.title()} pivot",
                state="observed",
                details=[{"label": "Query type", "value": queried_type}],
            )
            add_edge(subject_id, identifier_id, relation="searched_as", label="pivoted as")
        add_edge(
            identifier_id,
            node_id,
            relation="resolved_to",
            label="reported account"
            if _finding_node_type(finding) == "profile"
            else "reported finding",
            state=_node_state(finding),
            confidence=finding.confidence,
            support_count=max(1, len(finding.source_tools)),
        )

        relationship = finding.extra.get("relationship")
        if isinstance(relationship, dict):
            source_value = _clean_text(relationship.get("source"), limit=320)
            target_value = _clean_text(relationship.get("target"), limit=320)
            source_type = _clean_text(relationship.get("source_type"), limit=40).casefold()
            target_type = _clean_text(relationship.get("target_type"), limit=40).casefold()
            relation = _clean_text(relationship.get("kind"), limit=80).casefold()
            relation_label = _clean_text(relationship.get("label"), limit=120)
            if source_value and target_value and relation and relation_label:
                source_relation_id: str | None = None
                if source_type == "domain":
                    if (
                        investigation.query_type == "domain"
                        and source_value.casefold().rstrip(".")
                        == investigation.query.casefold().rstrip(".")
                    ):
                        source_relation_id = primary_identifier_id
                    else:
                        source_domain = source_value.casefold().rstrip(".")
                        source_relation_id = f"domain:{source_domain}"
                        add_node(
                            source_relation_id,
                            node_type="domain",
                            label=source_domain,
                            subtitle="Published relationship domain",
                            state="observed",
                            hidden_by_default=finding_hidden,
                        )

                target_relation_id: str | None = None
                if target_type == "domain":
                    target_domain = target_value.casefold().rstrip(".")
                    target_relation_id = f"domain:{target_domain}"
                    add_node(
                        target_relation_id,
                        node_type="domain",
                        label=target_domain,
                        subtitle="Published relationship domain",
                        state="observed",
                        hidden_by_default=finding_hidden,
                    )
                elif target_type == "email":
                    target_email = target_value.casefold()
                    target_relation_id = f"identifier:{_stable_token('email', target_email)}"
                    add_node(
                        target_relation_id,
                        node_type="email",
                        label=target_value,
                        subtitle="Published reporting address",
                        state="observed",
                        prominent=True,
                        hidden_by_default=False,
                    )

                if source_relation_id and target_relation_id:
                    add_edge(
                        source_relation_id,
                        target_relation_id,
                        relation=relation,
                        label=relation_label,
                        state="observed",
                        confidence=finding.confidence,
                        details=[
                            {"label": "Evidence", "value": finding.title or finding.finding_type},
                            {
                                "label": "Caution",
                                "value": "Published relationship evidence is not proof of common ownership.",
                            },
                        ],
                    )

        if finding.finding_type == "scholarly_profile":
            orcid = _clean_text(finding.extra.get("orcid"), limit=120)
            if orcid:
                orcid_value = orcid.rstrip("/").rsplit("/", 1)[-1]
                orcid_id = f"identifier:{_stable_token('orcid', orcid_value.casefold())}"
                add_node(
                    orcid_id,
                    node_type="identifier",
                    label=orcid_value,
                    subtitle="ORCID",
                    state="observed",
                    confidence=min(0.9, float(finding.confidence or 0.0) + 0.1),
                    links=[
                        {
                            "label": "Open ORCID",
                            "url": f"https://orcid.org/{orcid_value}",
                            "kind": "external",
                        }
                    ],
                    prominent=True,
                    hidden_by_default=False,
                )
                add_edge(
                    node_id,
                    orcid_id,
                    relation="scholarly_orcid",
                    label="reports ORCID",
                    state="candidate",
                    confidence=min(0.9, float(finding.confidence or 0.0) + 0.1),
                    details=[
                        {
                            "label": "Caution",
                            "value": "OpenAlex treats ORCID as a strong positive signal, but upstream assertions can still be wrong.",
                        }
                    ],
                )
            institutions = finding.extra.get("institutions")
            if isinstance(institutions, list):
                for raw_institution in institutions[:10]:
                    institution = _clean_text(raw_institution, limit=300)
                    if not institution:
                        continue
                    institution_id = f"scholarly-institution:{_stable_token(institution.casefold())}"
                    add_node(
                        institution_id,
                        node_type="finding",
                        label=institution,
                        subtitle="Scholarly affiliation",
                        state="candidate",
                        confidence=0.55,
                        hidden_by_default=finding_hidden,
                        details=[
                            {
                                "label": "Caution",
                                "value": "OpenAlex affiliations are derived from works and can be stale or misattributed.",
                            }
                        ],
                    )
                    add_edge(
                        node_id,
                        institution_id,
                        relation="scholarly_affiliation",
                        label="published affiliation",
                        state="candidate",
                        confidence=0.55,
                    )

        for email_address, email_source in _email_addresses_from_finding(finding):
            email_id = f"identifier:{_stable_token('email', email_address.casefold())}"
            add_node(
                email_id,
                node_type="email",
                label=email_address,
                subtitle="Email address",
                state=_node_state(finding),
                confidence=finding.confidence,
                details=[
                    {"label": "Evidence field", "value": email_source},
                    {
                        "label": "Sources",
                        "value": ", ".join(finding.source_tools or [finding.source_tool]),
                    },
                ],
                prominent=True,
                hidden_by_default=False,
            )
            add_edge(
                node_id,
                email_id,
                relation="reports_email",
                label="reports email",
                state="observed",
                confidence=finding.confidence,
                support_count=max(1, len(finding.source_tools)),
                details=[{"label": "Evidence field", "value": email_source}],
            )

        for tool_id in sorted(set(finding.source_tools or [finding.source_tool])):
            collector_id = f"collector:{tool_id.casefold()}"
            add_node(
                collector_id,
                node_type="collector",
                label=tool_id,
                subtitle="Collector provenance",
                state="observed",
                hidden_by_default=True,
            )
            add_edge(
                collector_id,
                node_id,
                relation="found_by",
                label="reported by",
                details=[
                    {
                        "label": "Raw evidence",
                        "value": finding.raw_output_reference or "run artifact",
                    }
                ],
            )

        domains = set(
            extract_domains(
                [
                    finding.extra.get("external_url"),
                    profile.get("biography"),
                    finding.description,
                ]
            )
        )
        canonical_host = _normalised_host(finding.canonical_url)
        if canonical_host:
            domains.add(canonical_host)
        for domain in sorted(domains):
            domain_id = f"domain:{domain}"
            add_node(
                domain_id,
                node_type="domain",
                label=domain,
                subtitle="Linked public domain",
                state="observed",
                links=[{"label": "Open domain", "url": f"https://{domain}", "kind": "external"}],
                hidden_by_default=finding_hidden,
            )
            add_edge(node_id, domain_id, relation="links_to", label="links to")

        canonical_url_key = (finding.canonical_url or "").casefold().rstrip("/")
        for linked in extract_profile_link_candidates(finding):
            if linked.url.casefold().rstrip("/") == canonical_url_key:
                continue
            linked_id = f"linked-profile:{_stable_token(linked.url.casefold())}"
            linked_label = (
                f"{linked.platform} · @{linked.account_identifier}"
                if linked.platform and linked.account_identifier
                else linked.platform or linked.host
            )
            add_node(
                linked_id,
                node_type="profile",
                label=linked_label,
                subtitle="Profile-link candidate",
                state="candidate",
                links=[{"label": "Open linked URL", "url": linked.url, "kind": "external"}],
                hidden_by_default=finding_hidden,
                details=[
                    {"label": "Source field", "value": linked.provenance},
                    {"label": "Linked host", "value": linked.host},
                ],
            )
            add_edge(
                node_id,
                linked_id,
                relation="explicit_profile_link",
                label="links to profile",
                state="candidate",
                details=[{"label": "Source field", "value": linked.provenance}],
            )

        if finding.finding_type == "telegram_message":
            export_sha = _clean_text(finding.extra.get("telegram_export_sha256"), limit=64)
            sender_id = _clean_text(finding.extra.get("telegram_sender_id"), limit=100)
            if sender_id:
                sender_node_id = f"identifier:{_stable_token('telegram_sender', sender_id.casefold())}"
                add_node(
                    sender_node_id,
                    node_type="identifier",
                    label=sender_id,
                    subtitle="Telegram sender ID",
                    state="observed",
                    details=[{"label": "Source", "value": "Telegram Desktop export"}],
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, sender_node_id, relation="sent_by", label="sent by")
            for username in finding.extra.get("telegram_usernames", [])[:50] if isinstance(finding.extra.get("telegram_usernames"), list) else []:
                handle = _clean_text(username, limit=32).removeprefix("@")
                if not handle:
                    continue
                username_id = f"identifier:{_stable_token('telegram_username', handle.casefold())}"
                add_node(
                    username_id,
                    node_type="username",
                    label=f"@{handle}",
                    subtitle="Telegram handle mention",
                    state="candidate",
                    links=[{"label": "Open Telegram", "url": f"https://t.me/{handle}", "kind": "external"}],
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, username_id, relation="mentions_username", label="mentions handle", state="candidate")
            for email in finding.extra.get("telegram_emails", [])[:50] if isinstance(finding.extra.get("telegram_emails"), list) else []:
                address = _clean_text(email, limit=320)
                if not address:
                    continue
                email_id = f"identifier:{_stable_token('email', address.casefold())}"
                add_node(email_id, node_type="email", label=address, subtitle="Telegram message email", state="observed", hidden_by_default=False)
                add_edge(node_id, email_id, relation="mentions_email", label="mentions email")
            for phone in finding.extra.get("telegram_phones", [])[:50] if isinstance(finding.extra.get("telegram_phones"), list) else []:
                value = _clean_text(phone, limit=40)
                if not value:
                    continue
                phone_id = f"identifier:{_stable_token('phone', value)}"
                add_node(phone_id, node_type="identifier", label=value, subtitle="Telegram message phone string", state="candidate", hidden_by_default=finding_hidden)
                add_edge(node_id, phone_id, relation="mentions_phone", label="mentions phone", state="candidate")
            for raw_url in finding.extra.get("telegram_urls", [])[:50] if isinstance(finding.extra.get("telegram_urls"), list) else []:
                public_url = _safe_external_url(raw_url)
                if not public_url:
                    continue
                telegram_ref = normalise_telegram_reference(public_url)
                if telegram_ref is not None:
                    url_id = f"telegram-ref:{_stable_token(telegram_ref.kind, telegram_ref.value.casefold())}"
                    node_type = "profile" if telegram_ref.kind in {"public_url", "username"} else "identifier"
                    label = (
                        f"Telegram · @{telegram_ref.value}"
                        if telegram_ref.kind in {"public_url", "username"}
                        else f"Telegram {telegram_ref.kind.replace('_', ' ')} · {telegram_ref.value}"
                    )
                    add_node(
                        url_id,
                        node_type=node_type,
                        label=label,
                        subtitle="Telegram reference from message",
                        state="candidate",
                        links=[{"label": "Open Telegram", "url": public_url, "kind": "external"}],
                        hidden_by_default=finding_hidden,
                    )
                    add_edge(node_id, url_id, relation="telegram_reference", label="references Telegram", state="candidate")
                    continue
                url_id = f"telegram-url:{_stable_token(public_url.casefold())}"
                add_node(
                    url_id,
                    node_type="document",
                    label=_clean_text(public_url, limit=120),
                    subtitle="Telegram message URL",
                    state="observed",
                    links=[{"label": "Open URL", "url": public_url, "kind": "external"}],
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, url_id, relation="mentions_url", label="mentions URL")
            media_reference = _clean_text(finding.extra.get("telegram_media_reference"), limit=1000)
            if media_reference:
                media_id = f"telegram-media:{_stable_token(str(export_sha or ''), media_reference)}"
                add_node(media_id, node_type="document", label=_clean_text(media_reference, limit=120), subtitle="Telegram attachment reference", state="observed", hidden_by_default=finding_hidden)
                add_edge(node_id, media_id, relation="has_media", label="has attachment")
            reply_to = _clean_text(finding.extra.get("telegram_reply_to_message_id"), limit=80)
            if export_sha and reply_to:
                target_node = telegram_message_nodes.get((export_sha, reply_to))
                if target_node:
                    add_edge(node_id, target_node, relation="replies_to", label="replies to")

        discord_guild_id = _clean_text(finding.extra.get("discord_guild_id"), limit=32)
        discord_channel_id = _clean_text(finding.extra.get("discord_channel_id"), limit=32)
        discord_guild_node_id = None
        discord_channel_node_id = None
        if discord_guild_id:
            guild_name = _clean_text(finding.extra.get("discord_guild_name"), limit=300)
            discord_guild_node_id = f"discord-guild:{_stable_token(discord_guild_id)}"
            add_node(
                discord_guild_node_id,
                node_type="identifier",
                label=guild_name or discord_guild_id,
                subtitle="Discord guild",
                state="observed",
                details=[{"label": "Guild ID", "value": discord_guild_id}],
                hidden_by_default=finding_hidden,
            )
            add_edge(
                node_id,
                discord_guild_node_id,
                relation="observed_in_guild",
                label="observed in guild",
            )
        if discord_channel_id:
            channel_name = _clean_text(finding.extra.get("discord_channel_name"), limit=300)
            discord_channel_node_id = f"discord-channel:{_stable_token(discord_guild_id or '@me', discord_channel_id)}"
            add_node(
                discord_channel_node_id,
                node_type="identifier",
                label=(f"#{channel_name}" if channel_name else discord_channel_id),
                subtitle="Discord channel",
                state="observed",
                details=[{"label": "Channel ID", "value": discord_channel_id}],
                hidden_by_default=finding_hidden,
            )
            if discord_guild_node_id:
                add_edge(
                    discord_guild_node_id,
                    discord_channel_node_id,
                    relation="contains_channel",
                    label="contains channel",
                )
            add_edge(
                node_id,
                discord_channel_node_id,
                relation="observed_in_channel",
                label="observed in channel",
            )

        if finding.finding_type == "discord_message":
            export_sha = _clean_text(finding.extra.get("discord_export_sha256"), limit=64)
            author_id = _clean_text(finding.extra.get("discord_author_id"), limit=32)
            if author_id:
                author_created = _clean_text(
                    finding.extra.get("discord_author_account_created_at"), limit=40
                )
                author_details = [{"label": "Source", "value": "Discord import"}]
                if author_created:
                    author_details.append({"label": "Account created", "value": author_created})
                author_node_id = f"identifier:{_stable_token('discord_author', author_id)}"
                add_node(
                    author_node_id,
                    node_type="identifier",
                    label=author_id,
                    subtitle="Discord user ID",
                    state="observed",
                    details=author_details,
                    links=[
                        {
                            "label": "Open Discord profile",
                            "url": f"https://discord.com/users/{author_id}",
                            "kind": "external",
                        }
                    ],
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, author_node_id, relation="sent_by", label="sent by")
            mentioned_ids = finding.extra.get("discord_mentioned_user_ids")
            for mentioned_id in (mentioned_ids[:50] if isinstance(mentioned_ids, list) else []):
                target_id = _clean_text(mentioned_id, limit=32)
                if not target_id:
                    continue
                mention_node_id = f"identifier:{_stable_token('discord_author', target_id)}"
                add_node(
                    mention_node_id,
                    node_type="identifier",
                    label=target_id,
                    subtitle="Discord user ID",
                    state="observed",
                    links=[
                        {
                            "label": "Open Discord profile",
                            "url": f"https://discord.com/users/{target_id}",
                            "kind": "external",
                        }
                    ],
                    hidden_by_default=finding_hidden,
                )
                add_edge(
                    node_id, mention_node_id, relation="mentions_user", label="mentions user"
                )
            usernames = finding.extra.get("discord_usernames")
            for username in (usernames[:50] if isinstance(usernames, list) else []):
                handle = _clean_text(username, limit=32).removeprefix("@")
                if not handle:
                    continue
                username_id = f"identifier:{_stable_token('discord_username', handle.casefold())}"
                add_node(
                    username_id,
                    node_type="username",
                    label=f"@{handle}",
                    subtitle="Discord handle mention",
                    state="candidate",
                    hidden_by_default=finding_hidden,
                )
                add_edge(
                    node_id,
                    username_id,
                    relation="mentions_username",
                    label="mentions handle",
                    state="candidate",
                )
            emails = finding.extra.get("discord_emails")
            for email in (emails[:50] if isinstance(emails, list) else []):
                address = _clean_text(email, limit=320)
                if not address:
                    continue
                email_id = f"identifier:{_stable_token('email', address.casefold())}"
                add_node(
                    email_id,
                    node_type="email",
                    label=address,
                    subtitle="Discord message email",
                    state="observed",
                    hidden_by_default=False,
                )
                add_edge(node_id, email_id, relation="mentions_email", label="mentions email")
            phones = finding.extra.get("discord_phones")
            for phone in (phones[:50] if isinstance(phones, list) else []):
                value = _clean_text(phone, limit=40)
                if not value:
                    continue
                phone_id = f"identifier:{_stable_token('phone', value)}"
                add_node(
                    phone_id,
                    node_type="identifier",
                    label=value,
                    subtitle="Discord message phone string",
                    state="candidate",
                    hidden_by_default=finding_hidden,
                )
                add_edge(
                    node_id, phone_id, relation="mentions_phone", label="mentions phone", state="candidate"
                )
            urls = finding.extra.get("discord_urls")
            for raw_url in (urls[:50] if isinstance(urls, list) else []):
                public_url = _safe_external_url(raw_url)
                if not public_url:
                    continue
                discord_ref = normalise_discord_reference(public_url)
                if discord_ref is not None:
                    ref_id = f"discord-ref:{_stable_token(discord_ref.kind, discord_ref.value.casefold())}"
                    ref_node_type = "profile" if discord_ref.kind in {"user_id", "invite"} else "identifier"
                    ref_label = f"Discord {discord_ref.kind.replace('_', ' ')} · {discord_ref.value}"
                    add_node(
                        ref_id,
                        node_type=ref_node_type,
                        label=ref_label,
                        subtitle="Discord reference from message",
                        state="candidate",
                        links=[{"label": "Open Discord", "url": public_url, "kind": "external"}],
                        hidden_by_default=finding_hidden,
                    )
                    add_edge(
                        node_id, ref_id, relation="discord_reference", label="references Discord", state="candidate"
                    )
                    continue
                url_id = f"discord-url:{_stable_token(public_url.casefold())}"
                add_node(
                    url_id,
                    node_type="document",
                    label=_clean_text(public_url, limit=120),
                    subtitle="Discord message URL",
                    state="observed",
                    links=[{"label": "Open URL", "url": public_url, "kind": "external"}],
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, url_id, relation="mentions_url", label="mentions URL")
            attachments = finding.extra.get("discord_attachments")
            for attachment in (attachments[:20] if isinstance(attachments, list) else []):
                filename = _clean_text(attachment, limit=300)
                if not filename:
                    continue
                attachment_id = f"discord-media:{_stable_token(str(export_sha or ''), filename)}"
                add_node(
                    attachment_id,
                    node_type="document",
                    label=_clean_text(filename, limit=120),
                    subtitle="Discord attachment reference",
                    state="observed",
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, attachment_id, relation="has_media", label="has attachment")
            reply_to = _clean_text(finding.extra.get("discord_reply_to_message_id"), limit=32)
            if export_sha and reply_to:
                target_node = discord_message_nodes.get((export_sha, reply_to))
                if target_node:
                    add_edge(node_id, target_node, relation="replies_to", label="replies to")

        if profile.get("avatar_url") or image_sha:
            image_links: list[dict[str, str]] = []
            if retained_avatar:
                image_links.append(
                    {
                        "label": "Open retained image",
                        "url": f"/findings/{finding.id}/avatar",
                        "kind": "internal",
                    }
                )
            if profile.get("avatar_url"):
                image_links.extend(
                    [
                        {
                            "label": "Google Lens",
                            "url": f"/findings/{finding.id}/reverse-image/google",
                            "kind": "internal",
                        },
                        {
                            "label": "Bing Visual Search",
                            "url": f"/findings/{finding.id}/reverse-image/bing",
                            "kind": "internal",
                        },
                    ]
                )
            image_key = str(image_sha or finding.id)
            image_id = f"image:{_stable_token(image_key)}"
            add_node(
                image_id,
                node_type="image",
                label="Profile image",
                subtitle=(
                    f"SHA-256 {str(image_sha)[:12]}…" if image_sha else finding.platform or "Avatar"
                ),
                state="observed",
                details=[
                    {"label": "SHA-256", "value": str(image_sha or "Not retained")},
                    {"label": "Account", "value": _finding_label(finding)},
                ],
                links=image_links,
                avatar_url=(f"/findings/{finding.id}/avatar" if retained_avatar else None),
                hidden_by_default=finding_hidden,
            )
            add_edge(node_id, image_id, relation="profile_image", label="uses profile image")

        wayback = finding.extra.get("wayback", {})
        if isinstance(wayback, dict):
            for role in ("first_capture", "latest_capture"):
                capture = wayback.get(role)
                if not isinstance(capture, dict) or not capture.get("captured_at"):
                    continue
                captured_at = _clean_text(capture.get("captured_at"), limit=80)
                archive_id = f"archive:{_stable_token(finding.id, role, captured_at)}"
                archive_url = _safe_external_url(capture.get("archive_url"))
                add_node(
                    archive_id,
                    node_type="archive",
                    label="First archived capture"
                    if role == "first_capture"
                    else "Latest distinct capture",
                    subtitle=captured_at[:10],
                    state="observed",
                    details=[
                        {"label": "Captured", "value": captured_at},
                        {"label": "Archive role", "value": role.replace("_", " ")},
                    ],
                    links=(
                        [{"label": "Open archived page", "url": archive_url, "kind": "external"}]
                        if archive_url
                        else []
                    ),
                    hidden_by_default=finding_hidden,
                )
                add_edge(node_id, archive_id, relation="archived_as", label="archived as")

    account_view = build_investigation_account_view(investigation)
    for account in account_view["accounts"]:
        if timeline_event_count >= 100:
            break
        source_finding_id = _clean_text(account.get("primary_finding_id"), limit=80)
        source_finding = finding_lookup.get(source_finding_id)
        source_node_id = f"finding:{source_finding_id}"
        if source_finding is None or source_node_id not in nodes:
            continue
        events = account.get("timeline_events", [])
        if not isinstance(events, list):
            continue
        for event in events:
            if timeline_event_count >= 100:
                break
            if not isinstance(event, dict) or event.get("include_in_timeline") is False:
                continue
            timestamp = _clean_text(event.get("timestamp"), limit=80)
            event_type = _clean_text(event.get("type"), limit=80)
            if not timestamp or not event_type:
                continue
            label = _clean_text(event.get("label"), limit=120) or event_type.replace(
                "_", " "
            ).title()
            account_key = _clean_text(account.get("key"), limit=240) or source_finding_id
            event_id = f"event:{_stable_token(account_key, event_type, timestamp)}"
            raw_sources = event.get("sources")
            event_sources = (
                [str(value) for value in raw_sources if value]
                if isinstance(raw_sources, list)
                else list(account.get("sources", []))
            )
            event_details = [
                {"label": "Timestamp", "value": timestamp},
                {"label": "Event type", "value": event_type.replace("_", " ")},
                {
                    "label": "Source field",
                    "value": _clean_text(event.get("source_field"), limit=120) or "unknown",
                },
                {
                    "label": "Collector sources",
                    "value": ", ".join(event_sources) or source_finding.source_tool,
                },
            ]
            if event.get("extractor"):
                event_details.append(
                    {"label": "Extractor", "value": _clean_text(event.get("extractor"))}
                )
            if event.get("confidence"):
                event_details.append(
                    {"label": "Semantic confidence", "value": _clean_text(event.get("confidence"))}
                )
            platform = _clean_text(account.get("platform"), limit=120) or "Account"
            add_node(
                event_id,
                node_type="event",
                label=label,
                subtitle=f"{platform} · {timestamp[:10]}",
                state="observed",
                details=event_details,
                links=_finding_links(investigation, source_finding),
                platform=platform,
                occurred_at=timestamp,
            )
            add_edge(
                source_node_id,
                event_id,
                relation="account_event",
                label="dated event",
                state="observed",
                details=[
                    {
                        "label": "Semantic",
                        "value": _clean_text(event.get("semantic"), limit=120)
                        or event_type.replace("_", " "),
                    }
                ],
            )
            timeline_event_count += 1

    if investigation.query_type == "email":
        for candidate in discover_username_pivots(investigation):
            username_id = f"identifier:{_stable_token('username', candidate.username.casefold())}"
            candidate_state = "observed" if candidate.followed else "candidate"
            add_node(
                username_id,
                node_type="username",
                label=candidate.username,
                subtitle="Possible username",
                state=candidate_state,
                details=[
                    {
                        "label": "Derived from",
                        "value": ", ".join(candidate.reasons) or "email evidence",
                    },
                    {
                        "label": "Platforms",
                        "value": ", ".join(candidate.platforms) or "Not specified",
                    },
                    {
                        "label": "Sources",
                        "value": ", ".join(candidate.source_tools) or "Email local part",
                    },
                    {
                        "label": "Followed by collectors",
                        "value": "Yes" if candidate.followed else "No",
                    },
                ],
                prominent=True,
                hidden_by_default=False,
            )
            source_ids = [
                f"finding:{finding_id}"
                for finding_id in candidate.finding_ids
                if finding_id in finding_ids
            ] or [primary_identifier_id]
            for source_id in source_ids:
                add_edge(
                    source_id,
                    username_id,
                    relation="candidate_username",
                    label="possible username",
                    state="observed",
                    support_count=max(1, len(candidate.finding_ids)),
                    details=[
                        {
                            "label": "Derivation",
                            "value": ", ".join(candidate.reasons) or "email evidence",
                        }
                    ],
                )

    for observation in sorted(
        investigation.location_observations,
        key=lambda item: (item.first_observed_at, item.id),
    ):
        place_key = "|".join(
            part.casefold()
            for part in (
                observation.place_name,
                observation.locality or "",
                observation.country or "",
            )
        )
        location_id = f"location:{_stable_token(place_key)}"
        source_url = _safe_external_url(observation.source_url)
        add_node(
            location_id,
            node_type="location",
            label=observation.place_name,
            subtitle=" · ".join(
                value
                for value in (observation.locality, observation.region, observation.country)
                if value
            ),
            state="candidate" if observation.evidence_type == "declared" else "observed",
            confidence=observation.confidence,
            details=[
                {"label": "Evidence type", "value": observation.evidence_type.replace("_", " ")},
                {"label": "Source", "value": observation.source_platform},
                {"label": "First observed", "value": observation.first_observed_at.isoformat()},
                {"label": "Privacy masked", "value": "Yes" if observation.privacy_masked else "No"},
            ],
            links=(
                [{"label": "Open source", "url": source_url, "kind": "external"}]
                if source_url
                else []
            ),
        )
        source_id = (
            f"finding:{observation.finding_id}"
            if observation.finding_id in finding_ids
            else subject_id
        )
        add_edge(
            source_id,
            location_id,
            relation="observed_at",
            label=observation.evidence_type.replace("_", " "),
            state="candidate" if observation.evidence_type == "declared" else "observed",
            confidence=observation.confidence,
        )

    for finding in investigation.findings:
        source_id = f"finding:{finding.id}"
        correlation_specs = (
            ("account_correlation", "related_account", "related account"),
            ("image_correlation", "same_image", "image match"),
            ("face_matching", "face_similarity", "face similarity"),
        )
        for metadata_key, relation, default_label in correlation_specs:
            metadata = finding.extra.get(metadata_key, {})
            matches = metadata.get("matches", []) if isinstance(metadata, dict) else []
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                peer_id = str(match.get("peer_id") or "")
                if peer_id not in finding_ids:
                    continue
                target_id = f"finding:{peer_id}"
                decision = match.get("decision", "unreviewed")
                if metadata_key == "account_correlation":
                    label = f"related account · {int(match.get('score', 0))}/100"
                    confidence = min(1.0, max(0.0, float(match.get("score", 0)) / 100))
                    signal_details = match.get("signals", [])
                    detail_value = ", ".join(
                        str(signal.get("type", "signal")).replace("_", " ")
                        for signal in signal_details
                        if isinstance(signal, dict)
                    )
                elif metadata_key == "image_correlation":
                    label = f"{match.get('match_type', default_label)} image match"
                    confidence = None
                    detail_value = f"Distance {match.get('distance', 'unknown')}"
                else:
                    similarity = float(match.get("similarity", 0))
                    label = f"face similarity · {similarity * 100:.1f}%"
                    confidence = similarity
                    detail_value = "Model similarity score; not an identity probability"
                add_edge(
                    source_id,
                    target_id,
                    relation=relation,
                    label=label,
                    state=_correlation_state(decision),
                    confidence=confidence,
                    details=[
                        {"label": "Decision", "value": str(decision)},
                        {"label": "Evidence", "value": detail_value or default_label},
                    ],
                )

    for node in nodes.values():
        data = node.get("data", {})
        if not isinstance(data, dict):
            continue
        node_type = str(data.get("type") or "")
        if node_type not in {"username", "email", "domain"}:
            continue
        label = _clean_text(data.get("label"), limit=320)
        if node_type == "username":
            label = label.removeprefix("@").strip()
        if not label:
            continue
        links = list(data.get("links") or [])
        links.append(
            {
                "label": "Collect this identifier",
                "url": f"/investigations/{investigation.id}/graph/collect?{urlencode({'query_type': node_type, 'value': label})}",
                "kind": "internal",
            }
        )
        data["links"] = links

    type_counts = Counter(node["data"]["type"] for node in nodes.values())
    state_counts = Counter(edge["data"]["state"] for edge in edges.values())
    return {
        "investigation": {
            "id": investigation.id,
            "title": investigation.title,
            "query": investigation.query,
            "query_type": investigation.query_type,
            "status": investigation.status,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_types": dict(sorted(type_counts.items())),
            "edge_states": dict(sorted(state_counts.items())),
            "saved_position_count": sum(1 for node in nodes.values() if "position" in node),
        },
    }

"""Compare retained positive evidence across explicitly selected cases, without collection."""

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.models.db import Finding, Investigation

MAX_CASES = 5
MAX_FINDINGS = 10_000
MAX_SIGNALS = 500
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")


@dataclass(frozen=True)
class ComparisonCase:
    id: str
    title: str
    query: str
    query_type: str
    positive_count: int


@dataclass(frozen=True)
class EvidenceReference:
    case_id: str
    case_title: str
    finding_id: str
    tool_run_id: str
    sources: tuple[str, ...]
    status: str
    observed_at: str
    source_url: str | None


@dataclass(frozen=True)
class SharedSignal:
    kind: str
    label: str
    value: str
    case_ids: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class CaseComparison:
    cases: tuple[ComparisonCase, ...]
    signals: tuple[SharedSignal, ...]
    total_shared_signals: int
    truncated: bool
    compared_at: str
    limitation: str = "Shared evidence is a correlation lead, not proof that cases concern the same person or operator."


def safe_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if parsed.port and (parsed.scheme, parsed.port) not in {("https", 443), ("http", 80)}:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def _email(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 254 or not EMAIL.fullmatch(value.strip()):
        return None
    local, domain = value.strip().rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _signals(finding: Finding) -> set[tuple[str, str, str]]:
    signals: set[tuple[str, str, str]] = set()
    url = safe_url(finding.canonical_url or finding.display_url)
    if url:
        signals.add(("url", "Shared URL", url))
    extra = finding.extra or {}
    profile = extra.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    if any(word in finding.finding_type for word in ("profile", "account")):
        account_id = profile.get("account_id") or extra.get("account_id")
        if finding.platform and isinstance(account_id, str | int) and not isinstance(account_id, bool):
            account_id = str(account_id).strip()
            if account_id and len(account_id) <= 200:
                signals.add(("account", "Same platform account ID", f"{finding.platform.casefold()}: {account_id}"))
    email_values = [extra.get("public_email"), profile.get("public_email")]
    if finding.finding_type in {"github_commit_identity", "github_profile_discovery"}:
        email_values.append(extra.get("email"))
    source_metadata = extra.get("source_metadata")
    if isinstance(source_metadata, dict):
        for metadata in list(source_metadata.values())[:50]:
            if isinstance(metadata, dict):
                email_values.append(metadata.get("public_email"))
                if finding.finding_type in {"github_commit_identity", "github_profile_discovery"}:
                    email_values.append(metadata.get("email"))
    if finding.finding_type in {"email", "email_address", "public_email"}:
        email_values.append(finding.value)
    for value in email_values:
        email = _email(value)
        if email:
            signals.add(("email", "Shared public email", email))
    cache = extra.get("image_cache")
    digest = cache.get("sha256") if isinstance(cache, dict) else None
    if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        signals.add(("image", "Identical image SHA-256", digest.lower()))
    if finding.finding_type == "github_ssh_signing_key":
        fingerprint = extra.get("fingerprint")
        if isinstance(fingerprint, str) and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint):
            signals.add(("key", "Shared public signing key", f"SSH {fingerprint}"))
    if finding.finding_type == "github_gpg_signing_key":
        key_hash = extra.get("public_key_sha256")
        if isinstance(key_hash, str) and re.fullmatch(r"[a-fA-F0-9]{64}", key_hash):
            signals.add(("key", "Shared public signing key", f"GPG public-key SHA-256: {key_hash.lower()}"))
    return signals


def compare_cases(investigations: list[Investigation]) -> CaseComparison:
    if not 2 <= len(investigations) <= MAX_CASES or len({item.id for item in investigations}) != len(investigations):
        raise ValueError(f"Select between 2 and {MAX_CASES} different cases to compare.")
    if sum(len(item.findings) for item in investigations) > MAX_FINDINGS:
        raise ValueError(f"Comparison supports at most {MAX_FINDINGS:,} retained findings across the selected cases.")
    grouped: dict[tuple[str, str, str], dict[tuple[str, str], EvidenceReference]] = {}
    cases = []
    for case in investigations:
        positives = [finding for finding in case.findings if finding.status in {"found", "likely"}]
        cases.append(ComparisonCase(case.id, case.title, case.query, case.query_type, len(positives)))
        for finding in positives:
            observed = finding.observed_at
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=UTC)
            reference = EvidenceReference(
                case.id, case.title, finding.id, finding.tool_run_id,
                tuple(sorted(set(finding.source_tools or [finding.source_tool]))),
                finding.status, observed.astimezone(UTC).isoformat(),
                safe_url(finding.canonical_url or finding.display_url or (finding.extra or {}).get("source_url")),
            )
            for signal in _signals(finding):
                grouped.setdefault(signal, {})[(case.id, finding.id)] = reference
    shared = []
    for (kind, label, value), references in grouped.items():
        case_ids = tuple(sorted({reference.case_id for reference in references.values()}))
        if len(case_ids) >= 2:
            shared.append(SharedSignal(kind, label, value, case_ids,
                                       tuple(sorted(references.values(), key=lambda item: (item.case_id, item.finding_id)))))
    priority = {"account": 0, "key": 1, "email": 2, "image": 3, "url": 4}
    shared.sort(key=lambda item: (-len(item.case_ids), priority[item.kind], item.value))
    return CaseComparison(tuple(cases), tuple(shared[:MAX_SIGNALS]), len(shared), len(shared) > MAX_SIGNALS,
                          datetime.now(UTC).isoformat())


def comparison_csv(report: CaseComparison) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["signal_type", "shared_value", "case_id", "case_title", "finding_id",
                     "tool_run_id", "source_tools", "status", "observed_at", "source_url"])
    for signal in report.signals:
        for evidence in signal.evidence:
            row = [signal.kind, signal.value, evidence.case_id, evidence.case_title,
                   evidence.finding_id, evidence.tool_run_id, ";".join(evidence.sources),
                   evidence.status, evidence.observed_at, evidence.source_url or ""]
            writer.writerow([f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value for value in row])
    return output.getvalue().encode("utf-8-sig")

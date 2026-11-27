from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.models.db import Investigation

CLUSTER_SETTING_PREFIX = "evidence_cluster_controls:"
DEFAULT_AUTO_CLUSTER_SCORE = 60


@dataclass(frozen=True, slots=True)
class EvidenceCluster:
    id: str
    finding_ids: tuple[str, ...]
    labels: tuple[str, ...]
    platforms: tuple[str, ...]
    source_tools: tuple[str, ...]
    rationale: tuple[dict[str, Any], ...]
    strongest_score: int
    note: str | None


def cluster_setting_key(investigation_id: str) -> str:
    return f"{CLUSTER_SETTING_PREFIX}{investigation_id}"


def _pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))  # type: ignore[return-value]


def normalise_cluster_controls(value: object, valid_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    forced_merges: set[tuple[str, str]] = set()
    forced_splits: set[tuple[str, str]] = set()
    for field, target in (("forced_merges", forced_merges), ("forced_splits", forced_splits)):
        items = value.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items[:500]:
            if not isinstance(item, list | tuple) or len(item) != 2:
                continue
            left, right = str(item[0]), str(item[1])
            if left in valid_ids and right in valid_ids and left != right:
                target.add(_pair(left, right))
    notes_raw = value.get("notes", {})
    notes: dict[str, str] = {}
    if isinstance(notes_raw, dict):
        for key, note in list(notes_raw.items())[:250]:
            cleaned = " ".join(str(note or "").split())[:2000]
            if cleaned:
                notes[str(key)[:80]] = cleaned
    return {
        "forced_merges": [list(pair) for pair in sorted(forced_merges)],
        "forced_splits": [list(pair) for pair in sorted(forced_splits)],
        "notes": notes,
    }


def _finding_label(finding: Any) -> str:
    profile = finding.extra.get("profile", {}) if isinstance(finding.extra, dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    return " ".join(
        str(
            profile.get("display_name")
            or profile.get("account_id")
            or finding.title
            or finding.value
            or finding.platform
            or finding.id
        ).split()
    )[:200]


def build_evidence_clusters(
    investigation: Investigation,
    controls: object = None,
    *,
    auto_score: int = DEFAULT_AUTO_CLUSTER_SCORE,
) -> list[EvidenceCluster]:
    findings = {finding.id: finding for finding in investigation.findings}
    valid_ids = set(findings)
    normalized = normalise_cluster_controls(controls, valid_ids)
    forced_merges = {_pair(*pair) for pair in normalized["forced_merges"]}
    forced_splits = {_pair(*pair) for pair in normalized["forced_splits"]}
    parent = {finding_id: finding_id for finding_id in valid_ids}

    def root(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[right_root] = left_root

    pair_evidence: dict[tuple[str, str], tuple[int, list[dict[str, Any]], str]] = {}
    for finding in findings.values():
        correlation = finding.extra.get("account_correlation", {})
        if not isinstance(correlation, dict):
            continue
        matches = correlation.get("matches", [])
        if not isinstance(matches, list):
            continue
        for match in matches[:500]:
            if not isinstance(match, dict):
                continue
            peer_id = str(match.get("peer_id") or "")
            if peer_id not in valid_ids or peer_id == finding.id:
                continue
            pair = _pair(finding.id, peer_id)
            score = int(match.get("score") or 0)
            signals = [dict(item) for item in match.get("signals", []) if isinstance(item, dict)][:20]
            decision = str(match.get("decision") or "unreviewed")
            existing = pair_evidence.get(pair)
            if existing is None or score > existing[0]:
                pair_evidence[pair] = (score, signals, decision)

    for pair, (score, _signals, decision) in pair_evidence.items():
        if pair in forced_splits or decision == "rejected":
            continue
        if pair in forced_merges or decision == "accepted" or score >= auto_score:
            union(*pair)
    for pair in forced_merges:
        if pair not in forced_splits:
            union(*pair)

    groups: dict[str, list[str]] = {}
    for finding_id in sorted(valid_ids):
        groups.setdefault(root(finding_id), []).append(finding_id)

    notes = normalized["notes"]
    clusters: list[EvidenceCluster] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_set = set(members)
        cluster_pairs = {
            pair: evidence
            for pair, evidence in pair_evidence.items()
            if pair[0] in member_set and pair[1] in member_set
        }
        signal_counts: Counter[str] = Counter()
        strongest_score = 0
        for score, signals, _decision in cluster_pairs.values():
            strongest_score = max(strongest_score, score)
            for signal in signals:
                signal_counts[str(signal.get("type") or "other")] += 1
        rationale = tuple(
            {"signal": signal, "supporting_pairs": count}
            for signal, count in signal_counts.most_common(8)
        )
        stable_payload = "\x1f".join(sorted(members)).encode()
        cluster_id = hashlib.sha256(stable_payload).hexdigest()[:16]
        labels = tuple(_finding_label(findings[item]) for item in members)
        platforms = tuple(sorted({str(findings[item].platform) for item in members if findings[item].platform}))
        source_tools = tuple(
            sorted(
                {
                    tool
                    for item in members
                    for tool in (findings[item].source_tools or [findings[item].source_tool])
                }
            )
        )
        clusters.append(
            EvidenceCluster(
                id=cluster_id,
                finding_ids=tuple(sorted(members)),
                labels=labels,
                platforms=platforms,
                source_tools=source_tools,
                rationale=rationale,
                strongest_score=strongest_score,
                note=notes.get(cluster_id),
            )
        )
    return sorted(clusters, key=lambda item: (-len(item.finding_ids), -item.strongest_score, item.id))


def update_pair_override(
    controls: object,
    valid_ids: set[str],
    left_id: str,
    right_id: str,
    action: str,
) -> dict[str, Any]:
    if left_id not in valid_ids or right_id not in valid_ids or left_id == right_id:
        raise ValueError("cluster override references invalid findings")
    normalized = normalise_cluster_controls(controls, valid_ids)
    merges = {_pair(*pair) for pair in normalized["forced_merges"]}
    splits = {_pair(*pair) for pair in normalized["forced_splits"]}
    pair = _pair(left_id, right_id)
    if action == "merge":
        merges.add(pair)
        splits.discard(pair)
    elif action == "split":
        splits.add(pair)
        merges.discard(pair)
    elif action == "reset":
        merges.discard(pair)
        splits.discard(pair)
    else:
        raise ValueError("invalid cluster override action")
    return {**normalized, "forced_merges": [list(item) for item in sorted(merges)], "forced_splits": [list(item) for item in sorted(splits)]}


def update_cluster_note(controls: object, valid_ids: set[str], cluster_id: str, note: str) -> dict[str, Any]:
    normalized = normalise_cluster_controls(controls, valid_ids)
    notes = dict(normalized["notes"])
    cleaned = " ".join(note.split())[:2000]
    if cleaned:
        notes[cluster_id[:80]] = cleaned
    else:
        notes.pop(cluster_id[:80], None)
    return {**normalized, "notes": notes}

from __future__ import annotations

import hashlib
from typing import Any

GRAPH_ANNOTATION_SETTING_PREFIX = "investigation_graph_annotations:"
MAX_NODE_NOTES = 500
MAX_MANUAL_RELATIONSHIPS = 250


def graph_annotation_setting_key(investigation_id: str) -> str:
    return f"{GRAPH_ANNOTATION_SETTING_PREFIX}{investigation_id}"


def normalise_graph_annotations(value: object, valid_node_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    notes: dict[str, str] = {}
    raw_notes = value.get("notes", {})
    if isinstance(raw_notes, dict):
        for node_id, note in list(raw_notes.items())[:MAX_NODE_NOTES]:
            node_id = str(node_id)
            cleaned = " ".join(str(note or "").split())[:2000]
            if node_id in valid_node_ids and cleaned:
                notes[node_id] = cleaned
    relationships: list[dict[str, str]] = []
    raw_relationships = value.get("relationships", [])
    if isinstance(raw_relationships, list):
        seen: set[str] = set()
        for item in raw_relationships[:MAX_MANUAL_RELATIONSHIPS]:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "")
            target = str(item.get("target") or "")
            label = " ".join(str(item.get("label") or "").split())[:120]
            note = " ".join(str(item.get("note") or "").split())[:1000]
            if source not in valid_node_ids or target not in valid_node_ids or source == target or not label:
                continue
            relationship_id = str(item.get("id") or "")[:64]
            if not relationship_id:
                relationship_id = hashlib.sha256(f"{source}\x1f{target}\x1f{label}".encode()).hexdigest()[:20]
            if relationship_id in seen:
                continue
            seen.add(relationship_id)
            relationships.append(
                {"id": relationship_id, "source": source, "target": target, "label": label, "note": note}
            )
    return {"notes": notes, "relationships": relationships}


def update_node_note(
    value: object, valid_node_ids: set[str], node_id: str, note: str
) -> dict[str, Any]:
    if node_id not in valid_node_ids:
        raise ValueError("graph note references an unknown node")
    normalized = normalise_graph_annotations(value, valid_node_ids)
    notes = dict(normalized["notes"])
    cleaned = " ".join(note.split())[:2000]
    if cleaned:
        notes[node_id] = cleaned
    else:
        notes.pop(node_id, None)
    return {**normalized, "notes": notes}


def add_manual_relationship(
    value: object,
    valid_node_ids: set[str],
    source: str,
    target: str,
    label: str,
    note: str = "",
) -> dict[str, Any]:
    if source not in valid_node_ids or target not in valid_node_ids or source == target:
        raise ValueError("manual relationship references invalid graph nodes")
    cleaned_label = " ".join(label.split())[:120]
    if not cleaned_label:
        raise ValueError("manual relationship label is required")
    normalized = normalise_graph_annotations(value, valid_node_ids)
    relationships = list(normalized["relationships"])
    relationship_id = hashlib.sha256(
        f"{source}\x1f{target}\x1f{cleaned_label}\x1f{len(relationships)}".encode()
    ).hexdigest()[:20]
    relationships.append(
        {
            "id": relationship_id,
            "source": source,
            "target": target,
            "label": cleaned_label,
            "note": " ".join(note.split())[:1000],
        }
    )
    return {**normalized, "relationships": relationships[-MAX_MANUAL_RELATIONSHIPS:]}


def remove_manual_relationship(
    value: object, valid_node_ids: set[str], relationship_id: str
) -> dict[str, Any]:
    normalized = normalise_graph_annotations(value, valid_node_ids)
    relationships = [
        item for item in normalized["relationships"] if item.get("id") != relationship_id
    ]
    return {**normalized, "relationships": relationships}


def apply_graph_annotations(graph: dict[str, Any], value: object) -> dict[str, Any]:
    nodes = graph.get("nodes", [])
    valid_node_ids = {
        str(node.get("data", {}).get("id"))
        for node in nodes
        if isinstance(node, dict) and isinstance(node.get("data"), dict)
    }
    normalized = normalise_graph_annotations(value, valid_node_ids)
    notes = normalized["notes"]
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            continue
        data = node["data"]
        node_id = str(data.get("id") or "")
        note = notes.get(node_id)
        if note:
            details = list(data.get("details") or [])
            details.append({"label": "Analyst note", "value": note})
            data["details"] = details
            data["analyst_note"] = note
    edges = graph.setdefault("edges", [])
    for relationship in normalized["relationships"]:
        edge_id = f"manual:{relationship['id']}"
        edges.append(
            {
                "data": {
                    "id": edge_id,
                    "source": relationship["source"],
                    "target": relationship["target"],
                    "relation": "analyst_relationship",
                    "label": relationship["label"],
                    "state": "observed",
                    "confidence": None,
                    "support_count": 1,
                    "details": [
                        {"label": "Created by", "value": "Analyst"},
                        *(
                            [{"label": "Analyst note", "value": relationship["note"]}]
                            if relationship.get("note")
                            else []
                        ),
                    ],
                    "manual": True,
                    "manual_relationship_id": relationship["id"],
                }
            }
        )
    summary = graph.setdefault("summary", {})
    summary["analyst_note_count"] = len(notes)
    summary["manual_relationship_count"] = len(normalized["relationships"])
    summary["edge_count"] = len(edges)
    return graph

from app.services.graph_annotations import (
    add_manual_relationship,
    apply_graph_annotations,
    remove_manual_relationship,
    update_node_note,
)


def _graph() -> dict[str, object]:
    return {
        "nodes": [
            {"data": {"id": "subject", "label": "Subject", "details": []}},
            {"data": {"id": "identifier:one", "label": "example", "details": []}},
        ],
        "edges": [],
        "summary": {"edge_count": 0},
    }


def test_graph_annotations_add_notes_and_manual_relationships_without_source_mutation() -> None:
    valid = {"subject", "identifier:one"}
    controls = update_node_note(None, valid, "identifier:one", "Analyst reviewed this handle")
    controls = add_manual_relationship(
        controls,
        valid,
        "subject",
        "identifier:one",
        "Known identifier",
        "Confirmed from case material",
    )
    graph = _graph()
    annotated = apply_graph_annotations(graph, controls)

    identifier = next(node["data"] for node in annotated["nodes"] if node["data"]["id"] == "identifier:one")
    assert identifier["analyst_note"] == "Analyst reviewed this handle"
    edge = annotated["edges"][0]["data"]
    assert edge["relation"] == "analyst_relationship"
    assert edge["label"] == "Known identifier"
    assert edge["manual"] is True
    assert annotated["summary"]["analyst_note_count"] == 1
    assert annotated["summary"]["manual_relationship_count"] == 1

    removed = remove_manual_relationship(controls, valid, edge["manual_relationship_id"])
    clean = apply_graph_annotations(_graph(), removed)
    assert clean["edges"] == []

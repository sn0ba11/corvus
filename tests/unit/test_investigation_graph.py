from datetime import UTC, datetime

from app.models.db import Finding, Investigation, LocationObservation, ToolRun
from app.services.investigation_graph import build_investigation_graph, graph_layout_setting_key


def test_graph_projection_keeps_entities_relationships_provenance_and_saved_positions() -> None:
    observed_at = datetime(2026, 8, 1, 18, 30, tzinfo=UTC)
    investigation = Investigation(
        id="11111111-1111-1111-1111-111111111111",
        title="Example subject",
        query="example",
        query_type="username",
        selected_tools=["maigret", "sherlock"],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="22222222-2222-2222-2222-222222222222",
        investigation_id=investigation.id,
        tool_id="maigret",
        display_name="Maigret",
        status="completed",
        timeout_seconds=60,
    )
    github = Finding(
        id="33333333-3333-3333-3333-333333333333",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="GitHub",
        value="example",
        canonical_url="https://github.com/example",
        status="found",
        confidence=0.94,
        extra={
            "profile": {
                "display_name": "Example Person",
                "avatar_url": "https://avatars.example.test/example.png",
                "biography": "Researcher at example.org · https://www.tiktok.com/@example.user",
                "account_id": "1234",
            },
            "image_cache": {"sha256": "a" * 64},
            "verification": {"state": "confirmed_account"},
            "external_url": "https://example.org/about",
            "timeline_events": [
                {
                    "type": "registered",
                    "label": "Account registered",
                    "timestamp": "2010-01-02T03:04:05+00:00",
                    "source_field": "created_at",
                    "semantic": "account_creation",
                    "confidence": "high",
                    "extractor": "GitHub API",
                }
            ],
            "wayback": {
                "first_capture": {
                    "captured_at": "2020-01-02T03:04:05+00:00",
                    "archive_url": "https://web.archive.org/web/20200102030405/https://github.com/example",
                },
                "latest_capture": {
                    "captured_at": "2025-06-07T08:09:10+00:00",
                    "archive_url": "https://web.archive.org/web/20250607080910/https://github.com/example",
                },
            },
            "account_correlation": {
                "matches": [
                    {
                        "peer_id": "44444444-4444-4444-4444-444444444444",
                        "decision": "accepted",
                        "score": 85,
                        "signals": [{"type": "username_exact", "weight": 35}],
                    }
                ]
            },
        },
        source_tool="maigret",
        source_tools=["maigret", "sherlock"],
        observed_at=observed_at,
        raw_output_reference="raw/maigret.json",
    )
    reddit = Finding(
        id="44444444-4444-4444-4444-444444444444",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="example",
        finding_type="profile",
        platform="Reddit",
        value="example",
        canonical_url="http://examiner:secret@127.0.0.1/private",
        status="likely",
        confidence=0.65,
        extra={
            "profile": {
                "display_name": "Example Person",
                "avatar_url": "https://avatars.example.test/not-retained.png",
            }
        },
        source_tool="sherlock",
        source_tools=["sherlock"],
        observed_at=observed_at,
    )
    location = LocationObservation(
        id="55555555-5555-5555-5555-555555555555",
        investigation_id=investigation.id,
        finding_id=github.id,
        place_name="Dublin",
        locality="Dublin",
        country="Ireland",
        evidence_type="declared",
        first_observed_at=observed_at,
        last_observed_at=observed_at,
        source_platform="GitHub",
        confidence=0.35,
        privacy_masked=False,
        raw_evidence={},
    )
    investigation.runs.append(run)
    investigation.findings.extend([github, reddit])
    investigation.location_observations.append(location)
    run.findings.extend([github, reddit])

    result = build_investigation_graph(
        investigation,
        saved_positions={"subject": {"x": 12.5, "y": -8}},
    )

    nodes = {node["data"]["id"]: node for node in result["nodes"]}
    edges = [edge["data"] for edge in result["edges"]]
    node_types = {node["data"]["type"] for node in result["nodes"]}

    assert nodes["subject"]["position"] == {"x": 12.5, "y": -8.0}
    assert {
        "subject",
        "username",
        "profile",
        "image",
        "domain",
        "location",
        "archive",
        "collector",
        "event",
    } <= node_types
    assert nodes[f"finding:{github.id}"]["data"]["state"] == "confirmed"
    assert nodes[f"finding:{github.id}"]["data"]["platform"] == "GitHub"
    assert nodes[f"finding:{github.id}"]["data"]["hidden_by_default"] is False
    assert nodes[f"finding:{reddit.id}"]["data"]["hidden_by_default"] is True
    assert not any(
        link.get("kind") == "external" for link in nodes[f"finding:{reddit.id}"]["data"]["links"]
    )
    assert nodes[f"finding:{reddit.id}"]["data"]["avatar_url"] is None
    event_nodes = [node["data"] for node in result["nodes"] if node["data"]["type"] == "event"]
    assert event_nodes[0]["label"] == "Account registered"
    assert event_nodes[0]["occurred_at"] == "2010-01-02T03:04:05+00:00"
    assert any(
        edge["relation"] == "found_by" and edge["target"] == f"finding:{github.id}"
        for edge in edges
    )
    assert any(edge["relation"] == "account_event" for edge in edges)
    assert any(edge["relation"] == "observed_at" and edge["state"] == "candidate" for edge in edges)
    assert any(
        edge["relation"] == "related_account" and edge["state"] == "confirmed" for edge in edges
    )
    linked_profiles = [
        node["data"]
        for node in result["nodes"]
        if node["data"].get("subtitle") == "Profile-link candidate"
    ]
    assert any(node["label"] == "TikTok · @example.user" for node in linked_profiles)
    assert any(edge["relation"] == "explicit_profile_link" for edge in edges)
    assert result["summary"]["node_count"] == len(result["nodes"])
    assert result["summary"]["edge_count"] == len(result["edges"])
    assert result["summary"]["saved_position_count"] == 1


def test_email_graph_promotes_addresses_and_possible_usernames() -> None:
    observed_at = datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
    investigation = Investigation(
        id="66666666-6666-6666-6666-666666666666",
        title="Email identity case",
        query="nova.example@example.org",
        query_type="email",
        selected_tools=["holehe"],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="77777777-7777-7777-7777-777777777777",
        investigation_id=investigation.id,
        tool_id="holehe",
        display_name="Holehe",
        status="completed",
        timeout_seconds=60,
    )
    finding = Finding(
        id="88888888-8888-8888-8888-888888888888",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="email",
        queried_value=investigation.query,
        finding_type="account",
        platform="Example Service",
        value="Registered account",
        canonical_url="https://accounts.example.org/profile/novaexample",
        status="found",
        confidence=0.8,
        extra={
            "public_email": "contact@example.org",
            "username": "novanq7az",
            "profile": {"account_id": "novaexample", "display_name": "Nova Example"},
        },
        source_tool="holehe",
        source_tools=["holehe"],
        observed_at=observed_at,
    )
    investigation.runs.append(run)
    investigation.findings.append(finding)
    run.findings.append(finding)

    graph = build_investigation_graph(investigation)
    nodes = [node["data"] for node in graph["nodes"]]
    edges = [edge["data"] for edge in graph["edges"]]
    email_nodes = {node["label"]: node for node in nodes if node["type"] == "email"}
    username_nodes = {node["label"]: node for node in nodes if node["type"] == "username"}

    assert {"nova.example@example.org", "contact@example.org"} <= set(email_nodes)
    assert email_nodes["contact@example.org"]["prominent"] is True
    assert {"nova.example", "novanq7az", "novaexample"} <= set(username_nodes)
    assert all(username_nodes[label]["prominent"] is True for label in username_nodes)
    assert any(
        edge["relation"] == "reports_email" and edge["state"] == "observed" for edge in edges
    )
    assert any(
        edge["relation"] == "candidate_username" and edge["state"] == "observed" for edge in edges
    )


def test_telegram_import_projects_identifiers_urls_media_and_reply_edges() -> None:
    observed_at = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    investigation = Investigation(
        id="99999999-9999-9999-9999-999999999999",
        title="Telegram case",
        query="example_user",
        query_type="username",
        selected_tools=[],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        investigation_id=investigation.id,
        tool_id="telegram_export_import",
        display_name="Telegram Desktop Export",
        status="completed",
        timeout_seconds=30,
    )
    first = Finding(
        id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="example_user",
        finding_type="telegram_message",
        platform="Telegram",
        value="Alice",
        status="found",
        confidence=1.0,
        extra={
            "telegram_export_sha256": "a" * 64,
            "telegram_message_id": "1",
            "telegram_sender_id": "user123",
            "telegram_usernames": ["bob_handle"],
            "telegram_emails": ["bob@example.org"],
            "telegram_phones": ["+353 87 123 4567"],
            "telegram_urls": ["https://t.me/otheruser", "https://example.org/page"],
            "telegram_media_reference": "files/report.pdf",
        },
        source_tool="telegram_export_import",
        source_tools=["telegram_export_import"],
        observed_at=observed_at,
    )
    second = Finding(
        id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="example_user",
        finding_type="telegram_message",
        platform="Telegram",
        value="Bob",
        status="found",
        confidence=1.0,
        extra={
            "telegram_export_sha256": "a" * 64,
            "telegram_message_id": "2",
            "telegram_reply_to_message_id": "1",
        },
        source_tool="telegram_export_import",
        source_tools=["telegram_export_import"],
        observed_at=observed_at,
    )
    investigation.runs.append(run)
    investigation.findings.extend([first, second])
    run.findings.extend([first, second])

    graph = build_investigation_graph(investigation)
    nodes = [node["data"] for node in graph["nodes"]]
    edges = [edge["data"] for edge in graph["edges"]]

    assert any(node["label"] == "@bob_handle" for node in nodes)
    assert any(node["label"] == "bob@example.org" for node in nodes)
    assert any(node["subtitle"] == "Telegram message phone string" for node in nodes)
    assert any(node["label"] == "Telegram · @otheruser" for node in nodes)
    assert any(node["subtitle"] == "Telegram attachment reference" for node in nodes)
    relations = {edge["relation"] for edge in edges}
    assert {"sent_by", "mentions_username", "mentions_email", "mentions_phone", "telegram_reference", "mentions_url", "has_media", "replies_to"} <= relations


def test_discord_import_projects_users_guild_channel_and_message_relationships() -> None:
    observed_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    investigation = Investigation(
        id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        title="Discord case",
        query="alice",
        query_type="username",
        selected_tools=[],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        investigation_id=investigation.id,
        tool_id="discord_export_import",
        display_name="DiscordChatExporter",
        status="completed",
        timeout_seconds=30,
    )
    profile = Finding(
        id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="alice",
        finding_type="profile",
        platform="Discord",
        value="alice",
        canonical_url="https://discord.com/users/175928847299117063",
        status="found",
        confidence=1.0,
        extra={
            "profile": {
                "display_name": "Alice",
                "account_id": "175928847299117063",
                "avatar_url": "https://cdn.discordapp.com/avatar.png",
            },
            "discord_entity_kind": "user",
            "discord_guild_id": "175928847299117000",
            "discord_guild_name": "Example Guild",
            "discord_channel_id": "175928847299117001",
            "discord_channel_name": "general",
        },
        source_tool="discord_export_import",
        source_tools=["discord_export_import"],
        observed_at=observed_at,
    )
    message = Finding(
        id="11111111-2222-3333-4444-555555555555",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="username",
        queried_value="alice",
        finding_type="discord_message",
        platform="Discord",
        value="Alice",
        status="found",
        confidence=1.0,
        extra={
            "discord_export_sha256": "b" * 64,
            "discord_message_id": "175928847299117099",
            "discord_author_id": "175928847299117063",
            "discord_author_username": "alice",
            "discord_guild_id": "175928847299117000",
            "discord_guild_name": "Example Guild",
            "discord_channel_id": "175928847299117001",
            "discord_channel_name": "general",
        },
        source_tool="discord_export_import",
        source_tools=["discord_export_import"],
        observed_at=observed_at,
    )
    investigation.runs.append(run)
    investigation.findings.extend([profile, message])
    run.findings.extend([profile, message])

    graph = build_investigation_graph(investigation)
    nodes = [node["data"] for node in graph["nodes"]]
    relations = {edge["data"]["relation"] for edge in graph["edges"]}

    assert any(node["subtitle"] == "Discord guild" and node["label"] == "Example Guild" for node in nodes)
    assert any(node["subtitle"] == "Discord channel" and node["label"] == "#general" for node in nodes)
    assert any(node["platform"] == "Discord" and node["label"] == "Alice" for node in nodes)
    assert {"contains_channel", "observed_in_guild", "observed_in_channel", "sent_by"} <= relations


def test_graph_projects_published_domain_relationships_as_direct_edges() -> None:
    observed_at = datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
    investigation = Investigation(
        id="aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb",
        title="example.com",
        query="example.com",
        query_type="domain",
        selected_tools=["domain_relationships"],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="cccccccc-1111-2222-3333-dddddddddddd",
        investigation_id=investigation.id,
        tool_id="domain_relationships",
        display_name="Certificate & Email Relationships",
        status="completed",
        timeout_seconds=60,
    )
    relationship = Finding(
        id="eeeeeeee-1111-2222-3333-ffffffffffff",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="domain",
        queried_value="example.com",
        finding_type="email_control_email",
        platform="Passive relationship",
        value="reports@vendor.test",
        status="found",
        confidence=0.8,
        title="DMARC rua reports to · reports@vendor.test",
        extra={
            "relationship": {
                "kind": "dmarc_reporting_address",
                "source": "example.com",
                "source_type": "domain",
                "target": "reports@vendor.test",
                "target_type": "email",
                "label": "DMARC rua reports to",
            }
        },
        source_tool="domain_relationships",
        source_tools=["domain_relationships"],
        observed_at=observed_at,
    )
    investigation.runs.append(run)
    investigation.findings.append(relationship)
    run.findings.append(relationship)

    graph = build_investigation_graph(investigation)
    nodes = {node["data"]["id"]: node["data"] for node in graph["nodes"]}
    edges = [edge["data"] for edge in graph["edges"]]

    source_id = next(
        node_id
        for node_id, node in nodes.items()
        if node["type"] == "domain" and node["label"] == "example.com"
    )
    target_id = next(
        node_id
        for node_id, node in nodes.items()
        if node["type"] == "email" and node["label"] == "reports@vendor.test"
    )
    assert any(
        edge["source"] == source_id
        and edge["target"] == target_id
        and edge["relation"] == "dmarc_reporting_address"
        for edge in edges
    )


def test_graph_projects_openalex_orcid_and_affiliations_as_candidate_pivots() -> None:
    observed_at = datetime(2026, 9, 17, 5, 30, tzinfo=UTC)
    investigation = Investigation(
        id="12345678-1111-2222-3333-abcdefghijkl",
        title="Example Researcher",
        query="Example Researcher",
        query_type="name",
        selected_tools=["openalex_scholar"],
        tags=[],
        status="completed",
        created_at=observed_at,
    )
    run = ToolRun(
        id="87654321-1111-2222-3333-lkjihgfedcba",
        investigation_id=investigation.id,
        tool_id="openalex_scholar",
        display_name="OpenAlex Scholarly Identity",
        status="completed",
        timeout_seconds=45,
    )
    finding = Finding(
        id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        investigation_id=investigation.id,
        tool_run_id=run.id,
        query_type="name",
        queried_value="Example Researcher",
        finding_type="scholarly_profile",
        platform="OpenAlex",
        value="A1234567890",
        canonical_url="https://openalex.org/A1234567890",
        status="likely",
        confidence=0.72,
        title="Example Researcher",
        extra={
            "profile": {"display_name": "Example Researcher"},
            "orcid": "https://orcid.org/0000-0002-1825-0097",
            "institutions": ["Example University", "Earlier Institute"],
        },
        source_tool="openalex_scholar",
        source_tools=["openalex_scholar"],
        observed_at=observed_at,
    )
    investigation.runs.append(run)
    investigation.findings.append(finding)
    run.findings.append(finding)

    graph = build_investigation_graph(investigation)
    nodes = [node["data"] for node in graph["nodes"]]
    edges = [edge["data"] for edge in graph["edges"]]

    assert any(node["subtitle"] == "ORCID" and node["label"] == "0000-0002-1825-0097" for node in nodes)
    assert any(node["subtitle"] == "Scholarly affiliation" and node["label"] == "Example University" for node in nodes)
    assert any(edge["relation"] == "scholarly_orcid" and edge["state"] == "candidate" for edge in edges)
    assert any(edge["relation"] == "scholarly_affiliation" and edge["confidence"] == 0.55 for edge in edges)


def test_graph_layout_setting_key_is_case_scoped() -> None:
    assert graph_layout_setting_key("case-id") == "investigation_graph_layout:case-id"

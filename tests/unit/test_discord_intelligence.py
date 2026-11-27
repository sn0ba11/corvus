import json
import sqlite3
from pathlib import Path

import pytest

from app.services.discord_intelligence import (
    DiscordImportError,
    normalise_discord_reference,
    parse_discord_chat_export,
    parse_discord_sqlite_export,
    snowflake_timestamp,
)


def test_snowflake_timestamp_derives_known_creation_time_offline() -> None:
    # Discord's own developer documentation example snowflake.
    derived = snowflake_timestamp("175928847299117063")
    assert derived is not None
    assert derived.isoformat() == "2016-04-30T11:18:25.796000+00:00"
    assert snowflake_timestamp(None) is None
    assert snowflake_timestamp("not-a-number") is None
    assert snowflake_timestamp("-1") is None


def test_normalise_discord_references() -> None:
    invite = normalise_discord_reference("https://discord.gg/AbCdEf12")
    assert invite is not None and invite.kind == "invite"
    assert invite.canonical_url == "https://discord.gg/AbCdEf12"
    long_invite = normalise_discord_reference("https://discord.com/invite/AbCdEf12")
    assert long_invite is not None and long_invite.kind == "invite"
    user = normalise_discord_reference("https://discord.com/users/175928847299117063")
    assert user is not None and user.kind == "user_id"
    bare_id = normalise_discord_reference("175928847299117063")
    assert bare_id is not None and bare_id.kind == "user_id"
    channel = normalise_discord_reference(
        "https://discord.com/channels/175928847299117063/175928847299117064"
    )
    assert channel is not None and channel.kind == "channel"
    dm_channel = normalise_discord_reference("https://discord.com/channels/@me/175928847299117064")
    assert dm_channel is not None and dm_channel.guild_id is None
    message = normalise_discord_reference(
        "https://discord.com/channels/175928847299117063/175928847299117064/175928847299117065"
    )
    assert message is not None and message.kind == "message"
    assert normalise_discord_reference("https://user:pass@discord.com/users/175928847299117063") is None
    assert normalise_discord_reference("https://example.org/users/175928847299117063") is None
    assert normalise_discord_reference("not-a-reference") is None


def test_parse_discord_chat_export_matches_upstream_field_names() -> None:
    raw = json.dumps(
        {
            "guild": {"id": "1", "name": "Example Guild"},
            "channel": {"id": "2", "type": "GuildTextChat", "name": "general"},
            "dateRange": {"after": None, "before": None},
            "exportedAt": "2026-08-17T09:00:00.000+00:00",
            "messages": [
                {
                    "id": "100",
                    "type": "Default",
                    "timestamp": "2026-08-17T09:00:00.000+00:00",
                    "timestampEdited": None,
                    "isPinned": False,
                    "content": "Contact @bob_handle bob@example.org https://discord.gg/AbCdEf12",
                    "author": {
                        "id": "175928847299117063",
                        "name": "alice",
                        "nickname": "Alice",
                        "avatarUrl": "https://cdn.discordapp.com/avatars/175928847299117063/avatar.png",
                        "isBot": False,
                    },
                    "attachments": [
                        {"id": "9", "url": "https://cdn.example/1", "fileName": "photo.png", "fileSizeBytes": 10}
                    ],
                    "mentions": [{"id": "175928847299117064", "name": "bob"}],
                },
                {
                    "id": "101",
                    "type": "Default",
                    "timestamp": "2026-08-17T09:01:00.000+00:00",
                    "content": "Reply",
                    "author": {"id": "175928847299117064", "name": "bob", "isBot": False},
                    "reference": {"type": "Default", "messageId": "100", "channelId": "2", "guildId": "1"},
                },
            ],
            "messageCount": 2,
        }
    ).encode()

    parsed = parse_discord_chat_export(raw)
    assert parsed.guild_name == "Example Guild"
    assert parsed.channel_name == "general"
    assert parsed.source == "chat_export"
    assert parsed.truncated is False
    first, second = parsed.messages
    assert first.author_id == "175928847299117063"
    assert first.author_username == "alice"
    assert first.author_name == "Alice"
    assert first.author_avatar_url == "https://cdn.discordapp.com/avatars/175928847299117063/avatar.png"
    assert first.usernames == ("bob_handle",)
    assert first.emails == ("bob@example.org",)
    assert "https://discord.gg/AbCdEf12" in first.urls
    assert first.attachments == ("photo.png",)
    assert first.mentioned_user_ids == ("175928847299117064",)
    assert second.reply_to_message_id == "100"
    assert len(parsed.sha256) == 64


def test_discord_chat_export_rejects_invalid_json_shape_and_oversize() -> None:
    with pytest.raises(DiscordImportError):
        parse_discord_chat_export(b"{}")
    with pytest.raises(DiscordImportError):
        parse_discord_chat_export(b"x" * (20 * 1024 * 1024 + 1))


def _build_discrawl_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            create table guilds (id text primary key, name text not null, raw_json text not null, updated_at text not null);
            create table channels (id text primary key, guild_id text not null, kind text not null, name text not null, raw_json text not null, updated_at text not null);
            create table members (guild_id text not null, user_id text not null, username text not null, global_name text, display_name text, nick text, raw_json text not null, updated_at text not null, primary key (guild_id, user_id));
            create table messages (id text primary key, guild_id text not null, channel_id text not null, author_id text, message_type integer not null, created_at text not null, edited_at text, content text not null, normalized_content text not null, reply_to_message_id text, raw_json text not null, updated_at text not null);
            create table message_attachments (attachment_id text primary key, message_id text not null, guild_id text not null, channel_id text not null, filename text not null, updated_at text not null);
            create table mention_events (event_id integer primary key autoincrement, message_id text not null, guild_id text not null, channel_id text not null, target_type text not null, target_id text not null, event_at text not null);
            """
        )
        connection.execute(
            "insert into guilds (id, name, raw_json, updated_at) values (?, ?, '{}', ?)",
            ("1", "Example Guild", "2026-08-17T09:00:00+00:00"),
        )
        connection.execute(
            "insert into channels (id, guild_id, kind, name, raw_json, updated_at) values (?, ?, ?, ?, '{}', ?)",
            ("2", "1", "text", "general", "2026-08-17T09:00:00+00:00"),
        )
        connection.execute(
            "insert into members (guild_id, user_id, username, display_name, raw_json, updated_at) values (?, ?, ?, ?, ?, ?)",
            (
                "1",
                "175928847299117063",
                "alice",
                "Alice",
                '{"avatarUrl":"https://cdn.discordapp.com/avatars/175928847299117063/avatar.png"}',
                "2026-08-17T09:00:00+00:00",
            ),
        )
        connection.executemany(
            "insert into messages (id, guild_id, channel_id, author_id, message_type, created_at, "
            "content, normalized_content, reply_to_message_id, raw_json, updated_at) "
            "values (?, ?, ?, ?, 0, ?, ?, ?, ?, '{}', ?)",
            [
                (
                    "100",
                    "1",
                    "2",
                    "175928847299117063",
                    "2026-08-17T09:00:00+00:00",
                    "Contact @bob_handle bob@example.org",
                    "contact @bob_handle bob@example.org",
                    None,
                    "2026-08-17T09:00:00+00:00",
                ),
                (
                    "101",
                    "1",
                    "2",
                    "175928847299117064",
                    "2026-08-17T09:01:00+00:00",
                    "Reply",
                    "reply",
                    "100",
                    "2026-08-17T09:01:00+00:00",
                ),
            ],
        )
        connection.execute(
            "insert into message_attachments (attachment_id, message_id, guild_id, channel_id, filename, updated_at) "
            "values ('a1', '100', '1', '2', 'photo.png', ?)",
            ("2026-08-17T09:00:00+00:00",),
        )
        connection.execute(
            "insert into mention_events (message_id, guild_id, channel_id, target_type, target_id, event_at) "
            "values ('100', '1', '2', 'user', '175928847299117064', ?)",
            ("2026-08-17T09:00:00+00:00",),
        )
        connection.commit()
    finally:
        connection.close()


def test_parse_discord_sqlite_export_matches_discrawl_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.sqlite"
    _build_discrawl_sqlite(db_path)
    raw = db_path.read_bytes()

    parsed = parse_discord_sqlite_export(raw, db_path)
    assert parsed.guild_name == "Example Guild"
    assert parsed.channel_name == "general"
    assert parsed.source == "discrawl_sqlite"
    assert len(parsed.messages) == 2
    first, second = parsed.messages
    assert first.author_username == "alice"
    assert first.author_name == "Alice"
    assert first.author_avatar_url == "https://cdn.discordapp.com/avatars/175928847299117063/avatar.png"
    assert first.usernames == ("bob_handle",)
    assert first.emails == ("bob@example.org",)
    assert first.attachments == ("photo.png",)
    assert first.mentioned_user_ids == ("175928847299117064",)
    assert second.reply_to_message_id == "100"


def test_discord_sqlite_export_rejects_non_sqlite_and_wrong_schema(tmp_path: Path) -> None:
    not_sqlite = tmp_path / "not.sqlite"
    not_sqlite.write_bytes(b"not a sqlite file" * 10)
    with pytest.raises(DiscordImportError):
        parse_discord_sqlite_export(not_sqlite.read_bytes(), not_sqlite)

    wrong_schema = tmp_path / "wrong.sqlite"
    connection = sqlite3.connect(wrong_schema)
    connection.execute("create table unrelated (id integer primary key)")
    connection.commit()
    connection.close()
    with pytest.raises(DiscordImportError):
        parse_discord_sqlite_export(wrong_schema.read_bytes(), wrong_schema)

    with pytest.raises(DiscordImportError):
        parse_discord_sqlite_export(b"x" * (150 * 1024 * 1024 + 1), wrong_schema)


def test_discord_sqlite_export_rejects_virtual_message_table(tmp_path: Path) -> None:
    db_path = tmp_path / "virtual.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE guilds (id TEXT)")
        connection.execute("CREATE TABLE channels (id TEXT)")
        connection.execute("CREATE VIRTUAL TABLE messages USING fts5(content)")
    finally:
        connection.close()

    with pytest.raises(DiscordImportError, match="expected discrawl schema"):
        parse_discord_sqlite_export(db_path.read_bytes(), db_path)

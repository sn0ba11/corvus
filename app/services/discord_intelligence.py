from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_SQLITE_BYTES = 150 * 1024 * 1024
MAX_MESSAGES = 5000
MAX_MESSAGE_TEXT = 8000
MAX_IDENTIFIERS_PER_MESSAGE = 50

# Discord's snowflake epoch: 2015-01-01T00:00:00.000Z, in Unix milliseconds.
DISCORD_EPOCH_MS = 1_420_070_400_000

SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9-]{2,32}$")
DISCORD_INVITE_HOSTS = frozenset({"discord.gg", "www.discord.gg"})
DISCORD_WEB_HOSTS = frozenset(
    {"discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"}
)
DISCORD_USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_.])@([a-z0-9_.]{2,32})(?![A-Za-z0-9_.])")
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}(?![\w.-])", re.IGNORECASE
)
PHONE_RE = re.compile(r"(?<!\d)(\+?[1-9][0-9 ()\-.]{6,20}[0-9])(?!\d)")
URL_RE = re.compile(r"https?://[^\s<>\"]{3,2048}", re.IGNORECASE)
SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_DISCRAWL_TABLES = frozenset({"guilds", "channels", "messages"})


class DiscordImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DiscordReference:
    kind: str  # "invite" | "user_id" | "channel" | "message"
    value: str
    canonical_url: str | None = None
    guild_id: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordMessage:
    message_id: str
    timestamp: datetime | None
    edited_timestamp: datetime | None
    author_id: str | None
    author_username: str | None
    author_name: str | None
    author_avatar_url: str | None
    author_is_bot: bool
    content: str
    reply_to_message_id: str | None
    attachments: tuple[str, ...]
    mentioned_user_ids: tuple[str, ...]
    usernames: tuple[str, ...]
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscordChatExport:
    guild_id: str | None
    guild_name: str | None
    channel_id: str | None
    channel_name: str | None
    channel_type: str | None
    messages: tuple[DiscordMessage, ...]
    sha256: str
    truncated: bool
    source: str


def snowflake_timestamp(snowflake: str | int | None) -> datetime | None:
    """Derive a Discord snowflake's creation time entirely offline.

    A Discord snowflake packs a millisecond timestamp (relative to Discord's own
    epoch) into its high 42 bits. No network access or third-party lookup is
    required or possible from the ID alone.
    """
    if snowflake is None or snowflake == "":
        return None
    try:
        value = int(snowflake)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value.bit_length() > 63:
        return None
    milliseconds = (value >> 22) + DISCORD_EPOCH_MS
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def normalise_discord_reference(value: str) -> DiscordReference | None:
    """Parse a bare snowflake, invite link or discord.com URL into a typed reference."""
    raw = value.strip()
    if not raw:
        return None
    if SNOWFLAKE_RE.fullmatch(raw):
        return DiscordReference("user_id", raw, f"https://discord.com/users/{raw}")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host in DISCORD_INVITE_HOSTS:
        if parts and INVITE_CODE_RE.fullmatch(parts[0]):
            return DiscordReference("invite", parts[0], f"https://discord.gg/{parts[0]}")
        return None
    if host not in DISCORD_WEB_HOSTS or not parts:
        return None
    if parts[0] == "invite" and len(parts) >= 2 and INVITE_CODE_RE.fullmatch(parts[1]):
        return DiscordReference("invite", parts[1], f"https://discord.com/invite/{parts[1]}")
    if parts[0] == "users" and len(parts) >= 2 and SNOWFLAKE_RE.fullmatch(parts[1]):
        return DiscordReference("user_id", parts[1], f"https://discord.com/users/{parts[1]}")
    if parts[0] == "channels" and len(parts) >= 3:
        guild_component, channel_component = parts[1], parts[2]
        if guild_component != "@me" and not SNOWFLAKE_RE.fullmatch(guild_component):
            return None
        if not SNOWFLAKE_RE.fullmatch(channel_component):
            return None
        guild_id = None if guild_component == "@me" else guild_component
        if len(parts) >= 4 and SNOWFLAKE_RE.fullmatch(parts[3]):
            return DiscordReference(
                "message",
                parts[3],
                raw[:2048],
                guild_id=guild_id,
                channel_id=channel_component,
            )
        return DiscordReference(
            "channel",
            channel_component,
            raw[:2048],
            guild_id=guild_id,
            channel_id=channel_component,
        )
    return None


def _flatten_content(value: object) -> str:
    return value[:MAX_MESSAGE_TEXT] if isinstance(value, str) else ""


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean_text(value: object, limit: int) -> str | None:
    text = " ".join(str(value or "").split())[:limit]
    return text or None


def _clean_url(value: str) -> str | None:
    candidate = value.rstrip(".,;:!?)]}")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return candidate[:2048]


def _extract_identifiers(
    text: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    usernames = tuple(
        dict.fromkeys(match.group(1) for match in DISCORD_USERNAME_RE.finditer(text))
    )[:MAX_IDENTIFIERS_PER_MESSAGE]
    emails = tuple(dict.fromkeys(match.group(0) for match in EMAIL_RE.finditer(text)))[
        :MAX_IDENTIFIERS_PER_MESSAGE
    ]
    phones: list[str] = []
    for match in PHONE_RE.finditer(text):
        raw = match.group(1).strip()
        digits = re.sub(r"\D", "", raw)
        if 7 <= len(digits) <= 15:
            phones.append(raw[:40])
    urls = tuple(
        dict.fromkeys(
            url for match in URL_RE.finditer(text) if (url := _clean_url(match.group(0))) is not None
        )
    )[:MAX_IDENTIFIERS_PER_MESSAGE]
    return usernames, emails, tuple(dict.fromkeys(phones))[:MAX_IDENTIFIERS_PER_MESSAGE], urls


def parse_discord_chat_export(raw: bytes) -> DiscordChatExport:
    """Parse a DiscordChatExporter JSON export.

    Field names match ``DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs`` in
    the upstream project (verified against source, not reverse-engineered from
    sample output): a root object with ``guild``/``channel``/``dateRange`` objects
    and a ``messages`` array of ``id``/``type``/``timestamp``/``timestampEdited``/
    ``content``/``author``/``attachments``/``mentions``/``reference`` fields.
    """
    if not raw or len(raw) > MAX_EXPORT_BYTES:
        raise DiscordImportError("Discord chat export must be a JSON file no larger than 20 MiB")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscordImportError("Discord chat export is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise DiscordImportError("Discord chat export must contain a top-level messages array")
    guild_raw = payload.get("guild")
    guild: dict[str, Any] = guild_raw if isinstance(guild_raw, dict) else {}
    channel_raw = payload.get("channel")
    channel: dict[str, Any] = channel_raw if isinstance(channel_raw, dict) else {}
    guild_id = _clean_text(guild.get("id"), 32)
    guild_name = _clean_text(guild.get("name"), 300)
    channel_id = _clean_text(channel.get("id"), 32)
    channel_name = _clean_text(channel.get("name"), 300)
    channel_type = _clean_text(channel.get("type"), 80)
    source_messages = payload["messages"]
    messages: list[DiscordMessage] = []
    for index, item in enumerate(source_messages[:MAX_MESSAGES]):
        if not isinstance(item, dict):
            continue
        message_id = _clean_text(item.get("id"), 32) or str(index)
        content = _flatten_content(item.get("content"))
        usernames, emails, phones, urls = _extract_identifiers(content)
        author_raw = item.get("author")
        author: dict[str, Any] = author_raw if isinstance(author_raw, dict) else {}
        author_id = _clean_text(author.get("id"), 32)
        author_username = _clean_text(author.get("name"), 80)
        author_name = _clean_text(
            author.get("nickname") or author.get("globalName") or author.get("name"), 300
        )
        author_avatar_url = None
        if isinstance(author.get("avatarUrl"), str):
            author_avatar_url = _clean_url(author["avatarUrl"])
        reference_raw = item.get("reference")
        reference: dict[str, Any] | None = reference_raw if isinstance(reference_raw, dict) else None
        reply_to = _clean_text(reference.get("messageId"), 32) if reference else None
        attachments_raw = item.get("attachments")
        attachments_list: list[Any] = attachments_raw if isinstance(attachments_raw, list) else []
        attachments = tuple(
            filename
            for attachment in attachments_list[:50]
            if isinstance(attachment, dict)
            and (filename := _clean_text(attachment.get("fileName"), 300))
        )
        mentions_raw = item.get("mentions")
        raw_mentions: list[Any] = mentions_raw if isinstance(mentions_raw, list) else []
        mentioned_ids = tuple(
            dict.fromkeys(
                mid
                for mention in raw_mentions[:50]
                if isinstance(mention, dict) and (mid := _clean_text(mention.get("id"), 32))
            )
        )
        messages.append(
            DiscordMessage(
                message_id=message_id,
                timestamp=_parse_timestamp(item.get("timestamp")),
                edited_timestamp=_parse_timestamp(item.get("timestampEdited")),
                author_id=author_id,
                author_username=author_username,
                author_name=author_name,
                author_avatar_url=author_avatar_url,
                author_is_bot=bool(author.get("isBot")),
                content=content,
                reply_to_message_id=reply_to,
                attachments=attachments,
                mentioned_user_ids=mentioned_ids,
                usernames=usernames,
                emails=emails,
                phones=phones,
                urls=urls,
            )
        )
    return DiscordChatExport(
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        channel_type=channel_type,
        messages=tuple(messages),
        sha256=digest,
        truncated=len(source_messages) > MAX_MESSAGES,
        source="chat_export",
    )


def validate_discord_sqlite_header(raw: bytes) -> None:
    if not raw or len(raw) > MAX_SQLITE_BYTES:
        raise DiscordImportError("Discord SQLite archive must be smaller than 150 MiB")
    if raw[:16] != SQLITE_HEADER:
        raise DiscordImportError("File is not a SQLite database")


def parse_discord_sqlite_export(raw: bytes, db_path: Path) -> DiscordChatExport:
    """Parse a discrawl-compatible SQLite archive.

    Table/column names match ``internal/store/sqlc/schema.sql`` in the upstream
    ``openclaw/discrawl`` project (verified against source). ``db_path`` must
    already contain exactly ``raw``'s bytes on disk, written by the caller before
    this validates and reads it — SQLite requires a real file, so validation of
    ``raw`` happens first and the connection is opened read-only/immutable
    afterwards.
    """
    validate_discord_sqlite_header(raw)
    digest = hashlib.sha256(raw).hexdigest()
    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise DiscordImportError("Unable to open the Discord SQLite archive") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA query_only = ON")
        tables = {
            str(row["name"])
            for row in connection.execute("PRAGMA main.table_list").fetchall()
            if row["type"] == "table"
        }
        if not REQUIRED_DISCRAWL_TABLES.issubset(tables):
            raise DiscordImportError(
                "SQLite archive does not match the expected discrawl schema "
                "(missing guilds/channels/messages tables)"
            )
        guild_row = connection.execute("SELECT id, name FROM guilds LIMIT 1").fetchone()
        guild_id = _clean_text(guild_row["id"], 32) if guild_row else None
        guild_name = _clean_text(guild_row["name"], 300) if guild_row else None

        member_names: dict[str, str] = {}
        member_usernames: dict[str, str] = {}
        member_avatars: dict[str, str] = {}
        if "members" in tables:
            for row in connection.execute(
                "SELECT user_id, username, global_name, display_name, nick, raw_json "
                "FROM members LIMIT 20000"
            ).fetchall():
                user_id = _clean_text(row["user_id"], 32)
                name = row["display_name"] or row["nick"] or row["global_name"] or row["username"]
                cleaned_name = _clean_text(name, 300)
                username = _clean_text(row["username"], 80)
                if user_id and cleaned_name:
                    member_names[user_id] = cleaned_name
                if user_id and username:
                    member_usernames[user_id] = username
                raw_json = row["raw_json"]
                if user_id and isinstance(raw_json, str):
                    try:
                        raw_member = json.loads(raw_json)
                    except json.JSONDecodeError:
                        raw_member = None
                    if isinstance(raw_member, dict):
                        avatar_value = raw_member.get("avatar_url") or raw_member.get("avatarUrl")
                        if isinstance(avatar_value, str) and (cleaned_avatar := _clean_url(avatar_value)):
                            member_avatars[user_id] = cleaned_avatar

        message_rows = connection.execute(
            "SELECT id, channel_id, author_id, created_at, edited_at, content, "
            "reply_to_message_id FROM messages ORDER BY created_at ASC LIMIT ?",
            (MAX_MESSAGES + 1,),
        ).fetchall()
        truncated = len(message_rows) > MAX_MESSAGES
        message_rows = message_rows[:MAX_MESSAGES]
        message_ids = [_clean_text(row["id"], 32) or "" for row in message_rows]

        attachments_by_message: dict[str, list[str]] = {}
        if message_ids and "message_attachments" in tables:
            placeholders = ",".join("?" for _ in message_ids)
            # The f-string only ever contains "?" placeholders derived from the bounded
            # message_ids count; every value is still bound as a query parameter below.
            query = f"SELECT message_id, filename FROM message_attachments WHERE message_id IN ({placeholders}) LIMIT 20000"  # noqa: S608
            for row in connection.execute(query, message_ids).fetchall():
                mid = _clean_text(row["message_id"], 32)
                filename = _clean_text(row["filename"], 300)
                if mid and filename:
                    attachments_by_message.setdefault(mid, []).append(filename)

        mentions_by_message: dict[str, list[str]] = {}
        if message_ids and "mention_events" in tables:
            placeholders = ",".join("?" for _ in message_ids)
            # Same bounded placeholder-only interpolation as above.
            query = f"SELECT message_id, target_id FROM mention_events WHERE message_id IN ({placeholders}) AND target_type = 'user' LIMIT 20000"  # noqa: S608
            for row in connection.execute(query, message_ids).fetchall():
                mid = _clean_text(row["message_id"], 32)
                target = _clean_text(row["target_id"], 32)
                if mid and target:
                    mentions_by_message.setdefault(mid, []).append(target)

        messages: list[DiscordMessage] = []
        channel_id: str | None = None
        for row in message_rows:
            channel_id = channel_id or _clean_text(row["channel_id"], 32)
            content = _flatten_content(row["content"])
            usernames, emails, phones, urls = _extract_identifiers(content)
            mid = _clean_text(row["id"], 32) or ""
            author_id = _clean_text(row["author_id"], 32)
            messages.append(
                DiscordMessage(
                    message_id=mid,
                    timestamp=_parse_timestamp(row["created_at"]),
                    edited_timestamp=_parse_timestamp(row["edited_at"]),
                    author_id=author_id,
                    author_username=member_usernames.get(author_id or ""),
                    author_name=member_names.get(author_id or ""),
                    author_avatar_url=member_avatars.get(author_id or ""),
                    author_is_bot=False,
                    content=content,
                    reply_to_message_id=_clean_text(row["reply_to_message_id"], 32),
                    attachments=tuple(attachments_by_message.get(mid, []))[:50],
                    mentioned_user_ids=tuple(dict.fromkeys(mentions_by_message.get(mid, [])))[:50],
                    usernames=usernames,
                    emails=emails,
                    phones=phones,
                    urls=urls,
                )
            )
        channel_name: str | None = None
        channel_type: str | None = None
        if channel_id:
            channel_row = connection.execute(
                "SELECT name, kind FROM channels WHERE id = ? LIMIT 1", (channel_id,)
            ).fetchone()
            if channel_row:
                channel_name = _clean_text(channel_row["name"], 300)
                channel_type = _clean_text(channel_row["kind"], 80)
    except sqlite3.Error as exc:
        raise DiscordImportError("Discord SQLite archive could not be read") from exc
    finally:
        connection.close()
    return DiscordChatExport(
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        channel_type=channel_type,
        messages=tuple(messages),
        sha256=digest,
        truncated=truncated,
        source="discrawl_sqlite",
    )


def message_metadata(message: DiscordMessage) -> dict[str, Any]:
    author_created = snowflake_timestamp(message.author_id) if message.author_id else None
    return {
        "discord_message_id": message.message_id,
        "discord_author_id": message.author_id,
        "discord_author_username": message.author_username,
        "discord_author_name": message.author_name,
        "discord_author_avatar_url": message.author_avatar_url,
        "discord_author_is_bot": message.author_is_bot,
        "discord_author_account_created_at": (
            author_created.isoformat() if author_created else None
        ),
        "discord_reply_to_message_id": message.reply_to_message_id,
        "discord_attachments": list(message.attachments),
        "discord_mentioned_user_ids": list(message.mentioned_user_ids),
        "discord_usernames": list(message.usernames),
        "discord_emails": list(message.emails),
        "discord_phones": list(message.phones),
        "discord_urls": list(message.urls),
        "timeline_events": (
            [
                {
                    "type": "discord_message",
                    "label": "Discord message",
                    "timestamp": message.timestamp.astimezone(UTC).isoformat(),
                    "source_field": "timestamp",
                    "semantic": "message_posted",
                    "confidence": "high",
                    "extractor": "Discord chat import",
                }
            ]
            if message.timestamp
            else []
        ),
    }

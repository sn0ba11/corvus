from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

MAX_EXPORT_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 5000
MAX_MESSAGE_TEXT = 8000
MAX_IDENTIFIERS_PER_MESSAGE = 50
USERNAME_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])")
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}(?![\w.-])", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(\+?[1-9][0-9 ()\-.]{6,20}[0-9])(?!\d)")
URL_RE = re.compile(r"https?://[^\s<>\"]{3,2048}", re.IGNORECASE)
NUMERIC_ID_RE = re.compile(r"^-?\d{4,20}$")
PUBLIC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
TELEGRAM_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})


class TelegramImportError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramReference:
    kind: str
    value: str
    canonical_url: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    message_id: str
    timestamp: datetime | None
    sender_name: str | None
    sender_id: str | None
    text: str
    reply_to_message_id: str | None
    media_reference: str | None
    usernames: tuple[str, ...]
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TelegramExport:
    chat_name: str
    chat_type: str | None
    chat_id: str | None
    messages: tuple[TelegramMessage, ...]
    sha256: str
    truncated: bool


def normalise_telegram_reference(value: str) -> TelegramReference | None:
    raw = value.strip()
    if not raw:
        return None
    if NUMERIC_ID_RE.fullmatch(raw):
        return TelegramReference("numeric_id", raw)
    username = raw.removeprefix("@").strip()
    if PUBLIC_USERNAME_RE.fullmatch(username):
        return TelegramReference("username", username, f"https://t.me/{username}")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in TELEGRAM_HOSTS:
        return None
    if parsed.username or parsed.password:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("+") and len(first) >= 8:
        token = first[1:]
        if re.fullmatch(r"[A-Za-z0-9_-]{7,128}", token):
            return TelegramReference("invite", token, f"https://t.me/+{token}")
        return None
    if first == "joinchat" and len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9_-]{7,128}", parts[1]):
        return TelegramReference("invite", parts[1], f"https://t.me/+{parts[1]}")
    if first == "c" and len(parts) >= 2 and parts[1].isdigit():
        return TelegramReference("private_channel_id", parts[1], f"https://t.me/c/{parts[1]}")
    if PUBLIC_USERNAME_RE.fullmatch(first):
        return TelegramReference("public_url", first, f"https://t.me/{first}")
    return None


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value[:MAX_MESSAGE_TEXT]
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value[:500]:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if sum(len(part) for part in parts) >= MAX_MESSAGE_TEXT:
            break
    return "".join(parts)[:MAX_MESSAGE_TEXT]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean_url(value: str) -> str | None:
    candidate = value.rstrip(".,;:!?)]}")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return candidate[:2048]


def _extract_identifiers(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    usernames = tuple(dict.fromkeys(match.group(1) for match in USERNAME_RE.finditer(text)))[:MAX_IDENTIFIERS_PER_MESSAGE]
    emails = tuple(dict.fromkeys(match.group(0) for match in EMAIL_RE.finditer(text)))[:MAX_IDENTIFIERS_PER_MESSAGE]
    phones: list[str] = []
    for match in PHONE_RE.finditer(text):
        raw = match.group(1).strip()
        digits = re.sub(r"\D", "", raw)
        if 7 <= len(digits) <= 15:
            phones.append(raw[:40])
    urls = tuple(dict.fromkeys(url for match in URL_RE.finditer(text) if (url := _clean_url(match.group(0))) is not None))[:MAX_IDENTIFIERS_PER_MESSAGE]
    return usernames, emails, tuple(dict.fromkeys(phones))[:MAX_IDENTIFIERS_PER_MESSAGE], urls


def parse_telegram_export(raw: bytes) -> TelegramExport:
    if not raw or len(raw) > MAX_EXPORT_BYTES:
        raise TelegramImportError("Telegram export must be a JSON file no larger than 20 MiB")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramImportError("Telegram export is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise TelegramImportError("Telegram export must contain a top-level messages array")
    chat_name = " ".join(str(payload.get("name") or payload.get("chat_name") or "Telegram export").split())[:300]
    chat_type = " ".join(str(payload.get("type") or "").split())[:80] or None
    chat_id = str(payload.get("id") or "").strip()[:80] or None
    messages: list[TelegramMessage] = []
    source_messages = payload["messages"]
    for index, item in enumerate(source_messages[:MAX_MESSAGES]):
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("id") or index)[:80]
        text = _flatten_text(item.get("text"))
        usernames, emails, phones, urls = _extract_identifiers(text)
        sender_name = " ".join(str(item.get("from") or item.get("actor") or "").split())[:300] or None
        sender_id = str(item.get("from_id") or item.get("actor_id") or "").strip()[:100] or None
        reply = str(item.get("reply_to_message_id") or item.get("reply_to") or "").strip()[:80] or None
        media = None
        for key in ("file", "photo", "media_type", "thumbnail", "sticker_emoji"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                media = value.strip()[:1000]
                break
        messages.append(TelegramMessage(message_id, _parse_timestamp(item.get("date") or item.get("date_unixtime")), sender_name, sender_id, text, reply, media, usernames, emails, phones, urls))
    return TelegramExport(chat_name, chat_type, chat_id, tuple(messages), digest, len(source_messages) > MAX_MESSAGES)


def message_metadata(message: TelegramMessage) -> dict[str, Any]:
    return {
        "telegram_message_id": message.message_id,
        "telegram_sender_name": message.sender_name,
        "telegram_sender_id": message.sender_id,
        "telegram_reply_to_message_id": message.reply_to_message_id,
        "telegram_media_reference": message.media_reference,
        "telegram_usernames": list(message.usernames),
        "telegram_emails": list(message.emails),
        "telegram_phones": list(message.phones),
        "telegram_urls": list(message.urls),
        "timeline_events": ([{
            "type": "telegram_message",
            "label": "Telegram message",
            "timestamp": message.timestamp.astimezone(UTC).isoformat(),
            "source_field": "date",
            "semantic": "message_posted",
            "confidence": "high",
            "extractor": "Telegram Desktop export",
        }] if message.timestamp else []),
    }

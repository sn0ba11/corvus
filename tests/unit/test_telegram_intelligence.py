import json

import pytest

from app.services.telegram_intelligence import (
    TelegramImportError,
    normalise_telegram_reference,
    parse_telegram_export,
)


def test_normalise_telegram_references() -> None:
    username = normalise_telegram_reference("@Example_User")
    assert username is not None and username.kind == "username"
    assert username.canonical_url == "https://t.me/Example_User"
    public_url = normalise_telegram_reference("https://telegram.me/example_user")
    assert public_url is not None and public_url.kind == "public_url"
    invite = normalise_telegram_reference("https://t.me/+AbC123456")
    assert invite is not None and invite.kind == "invite"
    numeric = normalise_telegram_reference("-1001234567890")
    assert numeric is not None and numeric.kind == "numeric_id"
    assert normalise_telegram_reference("https://user:pass@t.me/example_user") is None
    assert normalise_telegram_reference("https://example.org/example_user") is None


def test_parse_telegram_desktop_export_extracts_identifiers_and_relations() -> None:
    raw = json.dumps({
        "name": "Example Chat",
        "type": "public_supergroup",
        "id": -1001234567890,
        "messages": [
            {
                "id": 1,
                "date": "2026-08-17T09:00:00+00:00",
                "from": "Alice Example",
                "from_id": "user123",
                "text": "Contact @bob_handle bob@example.org +353 87 123 4567 https://example.org and https://t.me/otheruser",
                "file": "files/report.pdf",
            },
            {
                "id": 2,
                "date": "2026-08-17T09:01:00+00:00",
                "from": "Bob",
                "from_id": "user456",
                "reply_to_message_id": 1,
                "text": [{"type": "plain", "text": "Reply"}],
            },
        ],
    }).encode()

    parsed = parse_telegram_export(raw)
    assert parsed.chat_name == "Example Chat"
    assert parsed.chat_id == "-1001234567890"
    assert parsed.truncated is False
    first, second = parsed.messages
    assert first.sender_id == "user123"
    assert first.usernames == ("bob_handle",)
    assert first.emails == ("bob@example.org",)
    assert first.phones == ("+353 87 123 4567",)
    assert "https://t.me/otheruser" in first.urls
    assert first.media_reference == "files/report.pdf"
    assert second.reply_to_message_id == "1"
    assert len(parsed.sha256) == 64


def test_telegram_export_rejects_invalid_json_shape_and_oversize() -> None:
    with pytest.raises(TelegramImportError):
        parse_telegram_export(b"{}")
    with pytest.raises(TelegramImportError):
        parse_telegram_export(b"x" * (20 * 1024 * 1024 + 1))

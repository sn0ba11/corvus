# Browser and social evidence

[Documentation index](INDEX.md)

The managed browser is a separate local service included in the full stack. It supports Chromium and Firefox, named profiles, encrypted profile backup/restore and explicitly selected page captures. Keep its desktop service bound to loopback.

Use the Browser view to select a profile and review capture state. Captures retain provenance such as URL, timestamps, browser version, screenshot and bounded HTML artifacts. Login walls and challenges remain visible as limitations, not successful observations.

Supported collectors can optionally receive temporary, scoped browser-session exports. Turn session sharing off to keep those collectors anonymous. Protect browser profiles and encrypted backups as credential stores. Public Instagram browser observations and Instaloader API results are separate evidence paths; failure of one does not establish the absence of an account.

## Offline imports

Import an authorised Telegram Desktop JSON export or a DiscordChatExporter JSON / discrawl-compatible SQLite archive through the relevant case controls. These parsers work locally and do not contact Telegram or Discord. Imported message, sender and relationship information retains source provenance; attribution remains an analyst decision.

## Video evidence

The yt-dlp metadata action accepts supported public video URLs and does not download media. A separate explicit TikTok evidence action can retain an individual public video (up to 12 MiB), metadata, hashes and an acquisition manifest. Use its dedicated ZIP for large files that may exceed the ordinary evidence bundle's raw-file limit. Availability depends on upstream response.

See [managed browser](MANUAL.md#8-managed-sock-browser), [imports](MANUAL.md#15-telegram-and-discord-evidence-imports), and [privacy](PRIVACY.md).

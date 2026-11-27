# Third-party notices

Corvus application code is licensed under the PolyForm Noncommercial License 1.0.0. Third-party components, datasets and services are governed by their own terms.

## Bellingcat Name Variant Search and alias-generator

- Copyright: Stichting Bellingcat and contributors
- Projects: `bellingcat/name-variant-search` and `@bellingcat/alias-generator`
- Selected reference: `1caf0904d2e3620a49bab65a20e2af1fac7ca5e7`; alias-generator 4.0.0
- Licence: MIT

OSINT Workbench contains an independent, local Python implementation of the documented variation categories. It does not copy the nickname datasets, install the npm package, call Bellingcat services or send names outside the Workbench. The attribution is retained because the design and terminology were informed by the Bellingcat projects.

## Bellingcat Auto Archiver

- Copyright: Bellingcat and contributors
- Project: `bellingcat/auto-archiver`
- Selected reference: `afbe4fac508524d282328194ab1fa8d31cca6751`; release 1.2.7
- Licence: MIT

OSINT Workbench does not bundle or execute Auto Archiver. It can export an analyst-approved interoperability bundle containing a CSV feeder file, configuration, version pin, provenance manifest and checksums for separate execution by the operator.

## Toutatis account-recovery lookup methodology

- Project: `megadose/toutatis`
- Licence: GNU General Public License v3.0

Corvus contains an independent implementation of the Instagram account-recovery lookup technique documented by Toutatis. No Toutatis source code is copied or bundled. The Corvus implementation uses the existing bounded Instaloader probe/session path, treats lookup failure as non-fatal, exposes only the obfuscated recovery hints returned by Instagram and retains this notice because the upstream project informed the interoperability approach.

## TikTok-OSINT methodology reference

- Project: `Omicron166/TikTok-OSINT`
- Licence: MIT

Corvus does not bundle or execute TikTok-OSINT. The native TikTok public-profile collector is an independent implementation informed by the project's public-profile parsing approach. Corvus uses HTTPS, a current browser identity, the contemporary `__UNIVERSAL_DATA_FOR_REHYDRATION__` data path, bounded response handling and its own evidence/provenance model rather than the upstream parser.

## Steam-OSINT methodology reference

- Project: `Frontline-Femmes/Steam-OSINT`
- Licence: MIT
- Corresponding source: `https://github.com/Frontline-Femmes/Steam-OSINT`

Corvus does not bundle or execute Steam-OSINT. The native Steam collector is an independent implementation using Valve's documented Steamworks Web API endpoints for vanity-name resolution and public player summaries, with local key configuration, bounded JSON intake and Corvus-native evidence/provenance handling.

## Ignorant phone-presence methodology reference

- Project: `megadose/ignorant`
- Licence: GNU General Public License v3.0

Corvus implements selected documented patterns from Ignorant for the optional Snapchat and Amazon phone-presence checks. The tools are installed and licensed independently.

## DiscordChatExporter format compatibility

- Project: `Tyrrrz/DiscordChatExporter`
- Licence: MIT

Corvus does not bundle or execute DiscordChatExporter. The Discord chat-export importer reads the JSON export format produced by DiscordChatExporter, verified against `DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs` in the upstream source, so that an analyst-supplied export can be parsed locally without contacting Discord. No DiscordChatExporter source code is copied or bundled.

## discrawl schema compatibility

- Project: `openclaw/discrawl`
- Licence: MIT

Corvus does not bundle or execute discrawl. The Discord SQLite importer reads the table/column schema produced by discrawl, verified against `internal/store/sqlc/schema.sql` in the upstream source, so that an analyst-supplied archive can be parsed locally without contacting Discord. No discrawl source code is copied or bundled.

## gallery-dl

- Copyright: gallery-dl contributors
- Project: `mikf/gallery-dl`
- Selected reference: commit `26dfd4eea885577fef45e9b614538e5a7d24583a`
- Licence: GPL-2.0-only
- Corresponding source: `https://github.com/mikf/gallery-dl/tree/26dfd4eea885577fef45e9b614538e5a7d24583a`

Corvus runs gallery-dl only as a separate isolated tool container. The adapter is optional and
unchecked by default. It is constrained to bounded JSON metadata output with downloads disabled
and receives only a temporary Corvus-scoped Netscape cookie jar for Instagram/TikTok when the
operator has enabled managed-browser session sharing. It does not read browser profile databases.

## yt-dlp

- Copyright: yt-dlp contributors
- Project: `yt-dlp/yt-dlp`
- Selected reference: commit `f264a6cbd1f630894da0f368df00f8aae50140e0`
- Licence: Unlicense
- Corresponding source: `https://github.com/yt-dlp/yt-dlp/tree/f264a6cbd1f630894da0f368df00f8aae50140e0`

Corvus source-builds yt-dlp from the pinned Git revision in its own hardened tool container. The
case action is metadata-only (`--skip-download --dump-single-json --no-playlist`) and retains a
bounded field allowlist rather than yt-dlp's full format/download manifest. Corvus does not bundle
the PyInstaller executable. The selected Git/Python source distribution is under the Unlicense;
upstream documents additional licensing for some optional/release-binary components that Corvus
does not use here.

## Patchright

- Copyright: Kaliiiiiiiiii-Vinyzu and contributors
- Project: `Kaliiiiiiiiii-Vinyzu/patchright-python`
- Selected reference: version `1.61.2`
- Licence: Apache License 2.0
- Corresponding source: `https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python`

The browser-agent image bundles Patchright alongside standard Playwright. Patchright is a
drop-in-API-compatible fork of Playwright's Python async client with CDP-level patches that
reduce the chance a managed browser session is flagged as automated. It is opt-in and disabled
by default (`OSINT_STEALTH_BROWSER_ENABLED`); when off, the browser agent uses standard
Playwright exactly as before. Patchright does not solve or bypass a challenge once one is
shown — Corvus's existing manual-resume flow (screenshot, operator clears it in the managed
desktop, explicit audited resume) is unchanged and is still the only way a shown challenge is
cleared.

## Invisible Playwright

- Copyright: feder-cr and contributors
- Project: `feder-cr/invisible_playwright`
- Selected reference: version `0.6.1`
- Licence: MIT
- Corresponding source: `https://github.com/feder-cr/invisible_playwright`
- Patched engine source: `https://github.com/feder-cr/firefox_antidetect_patch`

The browser-agent image bundles Invisible Playwright as the selectable Firefox SOCK backend. Its patched Firefox binary is fetched during the image build, SHA-256 verified by the upstream package and retained inside the immutable image so runtime operation does not require downloading a browser. Corvus pairs each named Firefox SOCK identity with a deterministic seed and a dedicated persistent profile directory; Chromium and Firefox profile data are never mixed. Invisible Playwright changes the browser/automation fingerprint surface but does not solve or automatically clear CAPTCHAs, login walls or other shown challenges. Corvus retains the same explicit manual challenge/resume workflow and bounded egress policy for both engines.

## SearXNG

- Copyright: SearXNG contributors
- Project: `searxng/searxng`
- Selected reference: `c01178d03129d861582adf84a692e699f2f7ec05`; image version `2026.7.28-c01178d03`
- Licence: GNU Affero General Public License v3.0 or later
- Corresponding source: `https://github.com/searxng/searxng/tree/c01178d03129d861582adf84a692e699f2f7ec05`

OSINT Workbench builds a small derivative container from the official SearXNG image. The derivative includes the local-instance configuration, non-root bootstrap and modified Bing engine under the AGPL. The modified source is in `searxng/` and the full licence is in `LICENSES/AGPL-3.0.txt`. SearXNG remains a separate internal HTTP service, publishes no host port and is not linked into the proprietary application. The complete Workbench-side modifications are present in `searxng/`, while the exact upstream source revision is identified above and in `upstreams.lock.json`.

## metasearch2

- Copyright: metasearch2 contributors
- Project: `mat-1/metasearch2`
- Selected reference: `33c0b4b330e2f0cb13161a80cd80bed9f2c3008e`; application version `0.2.4`
- Licence: CC0-1.0

OSINT Workbench builds metasearch2 from the pinned upstream Git revision in a multi-stage container, enables its JSON API and runs it as a separate private backend/egress service with no host port. The local build applies the committed `metasearch2/engine-selection.patch`, which adds a bounded per-request engine selector so analyst settings control which upstream engines are actually queried. Corvus also applies bounded query packs and literal relevance checks before any metasearch2 result becomes investigation evidence.

## Simple Icons

- Copyright: Simple Icons Collaborators
- Project: `simple-icons/simple-icons`
- Selected reference: `34c22501f9ac9f22b12f825677ccbab1fb22e14b`
- Licence: CC0 1.0 Universal
- Corresponding source: `https://github.com/simple-icons/simple-icons/tree/34c22501f9ac9f22b12f825677ccbab1fb22e14b`

Corvus bundles a small, unmodified subset of Simple Icons SVGs (`app/static/icons/platforms/`) to show a
recognisable mark next to each platform in the dashboard and report instead of third-party favicons or a
live fetch. The icon artwork is CC0; the underlying brand names and logos remain the trademarks of their
respective owners, and display here is solely to identify the platform a finding came from. LinkedIn is
not included: Simple Icons' maintainers permanently removed it following a trademark enforcement request,
and Corvus does not source it from anywhere else. Platforms without a bundled icon fall back to a plain
text initials badge.

## Naminter

- Copyright (c) 2025 3xp0rt
- Licence: MIT
- Source and [upstream licence](https://github.com/3xp0rt/Naminter/blob/0cd7c9389dd6d1fbbd949b28005f7b334ca85bb9/LICENSE)

The locally built Naminter image retains its upstream licence and uses a JSON-only runner. The WhatsMyName dataset acquired at build time retains its separate CC BY-SA 4.0 licence.

## Distribution boundaries

Corvus release packages contain the Corvus source and launchers. Docker builds acquire upstream software separately on the user’s machine.

- Blackbird currently has no detected redistribution licence and must remain excluded from redistributed packages or images unless upstream permission/licensing is obtained.
- GPL/AGPL collectors and SearXNG remain separately licensed works/services. Any redistributed images or derivatives must satisfy their own source, notice and network-use obligations.
- WhatsMyName data remains CC-BY-SA-4.0 and carries its own attribution/share-alike requirements if redistributed.
- MIT/BSD/Apache components retain their notices and licence texts as required.
- The exact reviewed versions, isolation boundary and release restrictions are maintained in `docs/UPSTREAMS.md` and `upstreams.lock.json`.

## MIT licence text

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

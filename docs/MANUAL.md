# Corvus Operator Manual

**Corvus** is a local OSINT investigation workbench for authorised collection, review, correlation and reporting of publicly accessible information.

This manual describes normal operator use of the application. It is intended to ship with every Corvus build and should be updated whenever a user-visible workflow materially changes.

---

## Quick Start

If Corvus is already installed and running, the normal operator workflow is:

1. Open **Corvus** from the Desktop or Start Menu shortcut.
2. Enter the identifier you want to investigate: username, full name, email address, phone number or domain. If you know more than one, list the others under **Other known identifiers**.
3. Confirm the detected query type. Change it manually if Corvus guessed incorrectly.
4. Open **Collection and case options** only if you need to change the title, case reference, tags, collector selection, proxy, timeout or an investigation-specific toggle.
5. Click **Start full investigation**.
6. Watch the **Activity** panel. Collectors run independently; one failure does not automatically invalidate the rest of the case.
7. When collection finishes, review **Positive findings** first, then switch the finding-status filter to **All statuses** to see unresolved, blocked, rate-limited and negative records.
8. Use **Validate accounts** for broader profile-existence checking where appropriate.
9. Review **Accounts**, **Timeline**, **Graph**, **Search query workbench**, **Wayback**, **Location intelligence** and **Browser evidence capture** as required by the case.
10. Resolve any **manual action required** search challenges through the sock browser rather than treating them as negative findings.
11. Add or update **Investigator notes** and review correlation candidates before reporting.
12. Open **Report preview**, verify the evidence and limitations, then export HTML, JSON, CSV or a portable evidence bundle.

For a first username case, follow the complete walkthrough in **Section 4 — First investigation walkthrough**.

## Features at a glance

Corvus combines collection, analyst review, correlation and reporting in one local case workspace. The main operator-facing features are:

- **Typed investigations** — username, full name, email, phone number and domain workflows with compatible collectors selected automatically.
- **Several identifiers in one case** — enter every username, email address, name, phone number or domain you know for a subject and Corvus runs the right collectors for each.
- **Multi-collector username discovery** — Maigret, Sherlock, Blackbird, Nexfil and specialist public-profile collectors with source-specific parsing and confidence handling.
- **Extended username coverage** — optional Maigret all-sites and Naminter/WhatsMyName sweep for broader but slower/noisier coverage, at case start or on a finished case.
- **Public web search** — bounded SearXNG/Bing-style relevance-gated discovery plus a deterministic manual Search query workbench and a curated Google dork search page.
- **Automated social-platform dork run** — paced, multi-engine background searches through the sock browser for username, full-name, email and phone cases.
- **CAPTCHA/manual challenge workflow** — exact blocked searches can be handed to the visible sock browser and resumed without reconstructing the query.
- **Managed browser** — selectable Chromium or Firefox engines with persistent named identities, isolated per-engine profiles and encrypted backup/restore.
- **Authenticated session sharing** — narrowly scoped, temporary cookie sharing to supported collectors such as Instaloader and Maigret.
- **Profile validation** — analyst-triggered existence checking that distinguishes confirmed, missing, blocked and unresolved states.
- **Unified account view** — groups duplicate account evidence while retaining every underlying source.
- **Account/date semantics** — separates account creation, profile modification, third-party source refresh and Corvus observation dates.
- **Automatic and manual pivots** — bounded username, email, full-name, location, domain and infrastructure follow-up.
- **Investigation Graph** — interactive evidence map for identifiers, accounts, images, domains, URLs, locations, archives and provenance.
- **Image correlation** — exact SHA-256 and perceptual dHash comparison of retained profile images.
- **Local face matching** — YuNet/SFace candidate generation across retained avatars with analyst Accept/Reject/Reset review.
- **Transparent account correlation** — scores username, display-name, biography, location, domain and image signals with visible rationale.
- **Wayback / identity evolution** — automatic or manual archive checking with historical capture dates kept distinct from account dates.
- **Historical URL intelligence** — Common Crawl, OTX, URLScan and Wayback-oriented domain history views where configured.
- **Live Site Intelligence** — bounded same-domain GET-only crawling for public emails, documents, social links, external references, technology hints and tracking IDs.
- **Registration/infrastructure enrichment** — RDAP, explicit RIPEstat/ARIN public IP/ASN lookups, optional AbuseIPDB reputation, CRO/Companies House company evidence and VIES VAT validation.
- **Public video metadata** — explicit yt-dlp metadata-only extraction for supported public video URLs, including individual TikTok posts; this action does not download media. TikTok can refuse unauthenticated yt-dlp requests, so a failed extraction is inconclusive.
- **TikTok video evidence capture** — an analyst can explicitly acquire an individual public TikTok video in MP4 form (up to 12 MiB) with the pinned yt-dlp collector using Chrome impersonation. Corvus retains the original MP4, raw yt-dlp info JSON, a UTC acquisition timestamp, hashes and a case-linked manifest. Download the dedicated ZIP from the resulting evidence finding; files are hash-checked before packaging. No comments, private content, entire account feed or login credentials are collected. The normal portable evidence bundle may omit large videos due to its 10 MiB raw-file cap: use the dedicated TikTok ZIP for those videos. TikTok's response and displayed counters can change over time.
- **Location intelligence** — source-backed place observations, dates, precision, privacy masking, confidence and analyst-added location evidence.
- **Telegram import** — local parsing of Telegram Desktop JSON exports.
- **Discord import** — local parsing of DiscordChatExporter JSON and discrawl-compatible SQLite archives.
- **Retained artifact analysis** — local hashes, metadata, EXIF GPS and candidate identifier extraction without additional network requests.
- **Reverse-image handoff** — explicit analyst-approved Google Lens, Yandex, Bing and TinEye actions from retained images.
- **Auto Archiver handoff** — downloadable local bundle for Bellingcat Auto Archiver built from a positive or likely finding URL.
- **Investigator resource recommendations** — case-relevant official registers, filings and archive services opened only on analyst request.
- **Collector retry and cross-case comparison** — rerun one unsuccessful collector, and compare exact shared evidence across 2–5 finished cases.
- **Raw evidence and provenance** — execution logs, raw JSON, screenshots, HTML hashes, source URLs, versions and timestamps.
- **Reporting** — branded analytical report, HTML, JSON, CSV and portable evidence bundles with redaction controls and SHA-256 manifests.
- **What's New / update tracking** — build-range-aware change summaries and full changelog access.
- **Per-collector and runtime settings** — timeouts, engine selection, session sharing, Wayback automation, Tor isolation and optional API-backed collectors.

## UI orientation

Corvus is organised around a small number of persistent work areas rather than separate applications.

### Dashboard

The dashboard is the starting point. The upper search panel creates new investigations. **Collection and case options** expands the advanced controls. Beneath it, the investigation list provides quick access to existing cases and bulk deletion of finished cases.

### Investigation page

The investigation page is the main analytical workspace. From top to bottom it contains:

- **Case header and actions** — status, Graph, Validate accounts, sock-browser actions, raw JSON, retained-file analysis and Report.
- **Case navigation** — anchors to Activity, Graph, Accounts, Timeline, Historical URLs, Live Site, Technical findings, Next actions, Report and Settings where those features apply.
- **Metrics** — positive findings, validation coverage, location evidence and collector progress.
- **Activity** — live collector state and cancellation controls.
- **Detailed tool runs** — versions, configured timeouts, runtime, failures, execution logs and raw JSON.
- **Manual-action notices** — held search-engine challenges and exact retry/resume controls.
- **Technical findings** — account cards, source metadata, validation state, profile facts, images, archive information and correlation decisions.
- **Next actions** — Graph, Locations, Search query workbench, Google dork search, Wayback, Browser evidence capture and Investigator resource recommendations.
- **Advanced enrichment and correlation** — specialist pivots, image/face/account matching, retained-file analysis, infrastructure lookup and local Telegram/Discord imports.
- **Reporting** — preview and export controls.
- **Case management** — title, notes and deletion.

### Settings page

**Settings** contains deployment-wide defaults. These affect new work unless a case-level control overrides them. The complete Settings reference is in **Section 21**.

### Managed browser page

The Browser page controls the dedicated investigation browser. It shows login state, active browser engine, named identity profiles, encrypted profile backup/restore, manual URL capture and capture provenance. It is deliberately separate from the ordinary dashboard so authenticated browser state and captured evidence are explicit.

### Search query workbench

The Search query workbench builds deterministic Google/Bing/SearXNG query packs without automatically treating search results as evidence. Search results enter the case only through the supported automatic relevance gates or explicit analyst import.

### Graph, Locations, Wayback and Report

These are secondary analytical views of the same case evidence. They do not create a separate investigation. Analyst decisions made in these areas remain tied to the original case and provenance.

### Screenshot guide

## 1. Purpose and operating model

Corvus provides one case-oriented interface around multiple independent collectors, search engines, browser-assisted workflows and local evidence-processing tools. Each investigation has a typed seed such as a username, full name, email address, phone number or domain. Compatible collectors run independently and write normalised findings into the same case while retaining source-specific raw evidence and execution logs.

A search result or matching username is a lead, not proof that an account belongs to a subject. Corvus deliberately separates collection from attribution. The evidence register records what a source returned; the analyst decides what that means in context.

Corvus is designed for lawful, authorised searches of publicly accessible information. Do not use it to bypass access controls, obtain private data without authority, or automate behaviour that a target service prohibits.

## 2. Starting Corvus

On an installed Windows or macOS build, use the Corvus application shortcut. On a source/development installation, start the Docker Compose stack from the project directory.

The application dashboard is normally published to a loopback address selected by the installer. The managed sock browser is a separate local-only browser desktop with its own persistent browser profile.

The dashboard header shows the running Corvus version and build metadata. The **What's new** page lists user-visible changes introduced since the previously installed tracked build.

## 3. Creating an investigation

From the dashboard:

1. Enter the identifier to investigate.
2. Select or confirm the detected query type.
3. Give the case a useful title if the generated title is not sufficient.
4. Add a case reference, notes or tags where required.
5. Review the selected collectors.
6. Adjust collector timeouts only when there is a reason to do so.
7. Start the investigation.

Corvus selects only collectors compatible with the chosen query type. Specialist or higher-noise collectors may be deliberately off by default and can be enabled manually.

### Searching everything you know about one subject

A case has one primary search value, but you can give Corvus everything else you already know about the same subject. On the dashboard, put the main identifier in **Search value** and list the rest in **Other known identifiers**, one per line: usernames, email addresses, a full name, a phone number or a domain.

- The type is detected for each line. Start a line with `username:`, `name:`, `email:`, `phone:` or `domain:` to force it, for example `username: jane.example` for a dotted handle that would otherwise look like a domain. A leading `@` also marks a username.
- Each identifier runs the standard collectors for its own type inside the same case, so an email address gets the email collectors and a full name gets the name searches. All findings land in one evidence register, with each run recording which identifier it searched.
- Up to 8 additional identifiers can be added at a time. Duplicates and values already in the case are skipped.
- The collector checkboxes and per-case timeouts on the dashboard apply to the primary search value; additional identifiers use the standard collector set and the Settings timeouts.

On an existing case, the **Known identifiers in this case** panel lists what is being searched. Once collection has finished, **Add what else you know** and **Search these too** queue further identifiers without losing existing findings or runs.

Supplying several identifiers tells Corvus to search them together; it does not prove they belong to the same person. Treat a link between them as your working assumption and check it against the evidence.

Select **Username** explicitly for dotted handles if automatic detection proposes a domain. Your manual selection remains in effect while typing; **Use automatic type detection** restores automatic mode.

For username and email cases, **Automatically follow corroborated username/email identity pivots** is shown and checked by default. It runs a bounded two-hop expansion: corroborated usernames go through the enabled username collectors and independently observed public emails through email-capable collectors, with provenance kept for every hop.

### Username investigations

The standard username workflow uses several independent account-discovery collectors. Corvus normalises their results, groups duplicate account evidence and keeps source provenance.

Standard username searches include **Public Username Profiles**, which checks exact public accounts on GitHub, GitLab, Hacker News, Bluesky, Keybase, Chess.com, Lichess, Codeberg and Docker Hub. Source failures, rate limits and missing profiles are recorded separately. Rich profile results do not prove that the person being investigated owns those accounts.

Where the account owner lists their other accounts or websites, Corvus keeps them as linked accounts and linked sites. Examples are Keybase proofs, Chess.com's Twitch link, Lichess profile links and GitHub's X handle. These are claims made by the profile owner and are not independently confirmed. DEV Community is not queried, because it refuses Corvus's own User-Agent and Corvus does not disguise itself to get past that.

Email searches run **Gravatar Public Profile** and **OpenPGP Public Key** by default alongside Holehe and Blackbird; each is a single request to a public directory.

When automatic follow-ups are on, Corvus looks for them twice: once after the collectors finish, and again after profile verification. Verification can promote a weak lead to a found account and records the handle the platform confirmed, so handles that did not qualify the first time can qualify the second. Follow-ups from the second pass are not themselves verified in that run. The username and email follow-up lists show up to 20 candidates, each of which you can follow; automatic following only ever considers the first five.

Two further quick lookups run by default in username searches:

- **Snapchat Public Profile** finds ordinary accounts, which publish only a display name, snapcode and Bitmoji, as well as creator and business profiles. Creator profiles also give an address, creation and last-update dates, the owner's own "same as" links and related accounts.
- **Telegram Public Page** checks the public `t.me` page. For a channel it also reads the public post preview: the latest posts, their dates, outside links they share, and the photo, video and link counts.

When you run follow-up username searches (including automatic follow-ups), these quick exact lookups, along with TikTok, run alongside the existing tools. Handles found on an account page returned by a search, and on websites an account owner lists, appear as follow-up username candidates for the case. They are only candidates. Handles a profile states come first in the list, then search hits, then links found in channel posts. Automatic following still needs two independent sources that looked at the account itself: search results never count towards it, even from two search engines, and neither do links in channel posts. A handle a platform cannot hold (for example one with a dot on Telegram) is skipped for that lookup without a request and without a failed run.

An optional **Extended username search** adds the Maigret all-sites sweep and Naminter/WhatsMyName collection. This is deliberately opt-in because it is substantially slower and noisier than the standard bounded search. Use it when the standard pass does not provide enough coverage.

On a finished username case, the **Need wider username coverage?** panel offers **Run deeper username search**, which adds the same Maigret all-sites and Naminter/WhatsMyName sweep to the existing case. The case keeps its existing findings, logs and earlier attempts. The broader search may be slower and noisier; use the normal evidence controls when reviewing its leads.

### Full-name investigations

Full-name investigations generate bounded name variants and run dedicated name-search collectors. Exact-name evidence is required before search-engine results are retained automatically. Unrelated result pages are rejected rather than being admitted simply because the search engine ranked them.

### Email investigations

Email investigations can use services that test public account-presence or public profile associations. Candidate username pivots may be suggested from the email local part and supporting evidence. Automatic pivots are bounded and require corroboration.

### Phone investigations

Phone workflows are more sensitive to service behaviour and rate limits. Some platform-presence checks are optional and may be disabled by default. Treat `unknown`, `challenged` and `rate_limited` outcomes as unresolved, not negative findings.

### Domain investigations

Domain cases can combine passive subdomain discovery, historical URLs, registration/RDAP information and bounded live-site intelligence. Corvus limits high-volume passive sources so one collector cannot flood the evidence register with tens of thousands of weak one-source observations.

## 4. First investigation walkthrough: username to report

This walkthrough assumes Corvus is already installed and the operator is starting a normal username investigation.

### Step 1 — Start the case

On the dashboard, enter the username in **Search value**. Corvus usually detects **Username** automatically, but confirm the query-type selector before starting.

Open **Collection and case options** if you need to set any of the following:

- **Investigation title** — optional human-readable case title. If left blank, Corvus uses the search value.
- **Case reference** — optional external case/job reference.
- **Collection tools** — compatible collectors are shown and selected by default. Deselect only when you deliberately want to omit a collector.
- **Timeout** beside each collector — maximum runtime for that collector in this investigation. This overrides the Settings default for this case only.
- **Proxy** — Disabled, configured HTTP, configured HTTPS, configured SOCKS or configured Tor. Use only when the deployment is configured for that proxy and the case requires it.
- **Tags** — comma-separated case labels.
- **Extended username search** — username cases only; off by default. Adds the Maigret all-sites and Naminter/WhatsMyName sweep when the normal bounded username pass is insufficient. It is slower and noisier.

Click **Start full investigation**.

### Step 2 — Watch collection rather than assuming it is stuck

The investigation page opens immediately. The **Activity** panel shows what is queued, running and completed. Each collector is independent.

Use **Detailed tool runs** when something looks wrong. For each run, check:

- collector name/version;
- configured timeout;
- actual runtime;
- status;
- rate-limit flag;
- **Execution log**;
- **Raw JSON**.

If one collector fails while the others continue, let the investigation finish unless there is a case-specific reason to cancel it.

### Step 3 — Handle CAPTCHA or search-engine blocks correctly

If Corvus displays **manual action required**, open the specific blocked search through **Open and clear <engine>**. The managed sock browser opens the held search flow.

Complete the visible challenge manually. Then use **Resume after manual action**. If the exact search remains blocked, use **Abandon** and later **Retry exact <engine> search**. A challenged search is unresolved; it is not a negative finding.

### Step 4 — Review positive findings first

The findings area defaults to **Positive only**. Review the account cards and source provenance rather than relying on the headline count.

For each relevant finding check:

- platform and canonical profile URL;
- source collector(s);
- display name, account ID and biography where present;
- profile image and whether it was retained locally;
- follower/following or platform-specific fields;
- account-created, source-updated and Corvus observation dates;
- validation/existence state;
- confidence and source-specific limitations;
- Wayback information when automatic lookup is enabled.

Use **View parsed record** to inspect the normalised JSON record for that finding. Use **View raw JSON** at case/run level when you need the original collector response.

### Step 5 — Review everything Corvus did not classify as positive

Change the status filter from **Positive only** to **All statuses**. This is important. It exposes unknown, not-found, rate-limited and error records that may explain gaps in coverage.

Use the text filter to narrow findings by platform or tool. **Group by platform** is enabled by default; turn it off when chronological/source-level review is easier without grouping.

### Step 6 — Validate profile claims

When the initial collection has finished, use **Validate accounts**. This performs broader existence checking against candidate profile URLs.

Interpret results carefully:

- **confirmed** — the checked profile matched the expected account evidence;
- **likely** — supporting evidence exists but is not definitive;
- **missing/not found** — the validation path indicates the page/account is absent;
- **blocked/unresolved** — the source could not be conclusively checked.

Blocked or unresolved is not the same as missing.

### Step 7 — Review account grouping and correlation

If the case has multiple findings that appear to represent the same account or person, review the unified account view and any correlation candidates.

Available analyst controls include:

- **Accept / Reject / Reset** for image matches;
- **Accept / Reject / Reset** for face-similarity candidates;
- **Accept / Reject / Reset** for related-account scoring candidates;
- **Force merge / Force split / Reset pair override** for account clusters;
- cluster-level analyst notes.

These controls record analyst judgement. They do not alter the underlying source records.

### Step 8 — Follow useful pivots

Open **Next actions** and use only the pivots justified by the evidence.

For username cases this can include:

- **Full-name pivots** extracted from positive profile evidence;
- **Email pivots** from explicit public/contact email fields;
- **Search query workbench** for exact identifiers, platform restrictions, date ranges, documents and aliases;
- **Wayback and identity evolution** for historical profile captures;
- **Browser evidence capture** for pages that require rendering, authentication or manual review;
- **Location intelligence** where source evidence supports a location lead.

Masked recovery hints are not valid email pivots.

Other follow-up tools in the same area:

- **Google dork search** — curated Google query packs for the case, each with **Open Google**, **Copy query** and **Copy link**. Queries are labelled **Documented** or **Best effort** according to whether Google currently documents the operators used. Opening or copying a search does not create a finding.
- **Investigator resource recommendations** — official registers, filings and archive services relevant to the case. Nothing is contacted until you choose **Open**, which opens the external site in a new tab and records the launch in the audit log. An entry marked **launch sends case data** passes the case search value to that external site; open it only when that disclosure is acceptable for the case.
- **Possible username variants** — name cases only. Corvus generates candidate handles locally from the supplied name; no network request runs until you choose **Run selected variant** for one of them, and only once the current run has finished.
- **Auto Archiver bundle** — shown on positive and likely findings that have a public URL. It downloads a ZIP for Bellingcat Auto Archiver containing `urls.csv`, `orchestration.yaml`, a README and a hashed manifest. This is a local handoff only: Corvus does not run Auto Archiver or submit the URL anywhere.

### Step 9 — Use specialist enrichment only when it adds value

Under **Advanced enrichment and correlation**, the operator can:

- **Enrich public profiles** — bounded fetching of public profile metadata. **Respect robots.txt** is checked by default; disabling it is audited.
- **Correlate profile images** — local SHA-256/dHash comparison. **Distance threshold** controls perceptual-image tolerance; lower is stricter.
- **Match faces locally** — local YuNet/SFace comparison. **Minimum cosine similarity** sets the candidate threshold. The score is not an identity probability.
- **Correlate accounts** — transparent multi-signal scoring. **Minimum score** controls which candidates are surfaced.
- **Analyze retained files** — hashes, metadata, EXIF GPS and candidate identifiers from already-retained files. No network request is made.
- **Infrastructure registry / reputation lookup** — explicit public IP/ASN lookup using RIPEstat or ARIN RDAP; when configured, AbuseIPDB adds a passive IP-only 90-day reputation check.
- **Validate EU VAT number** — submits one analyst-supplied EU/Northern Ireland VAT number to the official VIES validation service and retains the returned validation record.
- **Extract public video metadata** — runs pinned yt-dlp in metadata-only mode against one analyst-supplied supported public video URL; no media download is requested.
- **Import Telegram Desktop export** — local JSON parsing.
- **Import Discord chat export** — local DiscordChatExporter JSON parsing.
- **Import Discord archive** — local discrawl-compatible SQLite parsing.

Do not run specialist tools merely because they exist; use them when they answer a case question.

### Step 10 — Add investigator context

Open **Edit title and notes** near the bottom of the case. Keep notes factual and distinguish observations from analyst conclusions. These notes appear near the top of the analytical report.

### Step 11 — Preview the report

Open **Report preview**. Review:

- executive overview;
- investigator notes;
- unified account assessment;
- platform/account cards;
- location summary where present;
- collector/run provenance;
- browser provenance;
- technical evidence register;
- limitations and unresolved states.

Correct obvious false-positive interpretations before exporting. Preserve contradictory or unresolved evidence rather than deleting it simply because it does not support the working hypothesis.

### Step 12 — Export

The reporting area provides:

- **Printable HTML** — self-contained analytical report suitable for printing/PDF generation.
- **JSON evidence** — complete structured case record.
- **Raw collector JSON** — retained upstream/source artifacts.
- **CSV findings** — flat finding register.
- **Portable evidence bundle** — ZIP with selected structured evidence, optional bounded raw artifacts and SHA-256 manifest.

Evidence-bundle controls are:

- **Include bounded raw case artifacts** — checked by default. Includes retained raw artifacts subject to bundle limits.
- **Redact identifiers** — removes/replaces identifiers in the portable bundle where supported.
- **Redact notes** — excludes investigator notes.
- **Redact external URLs** — removes external URLs from the portable bundle.
- **Select individual findings** — include only checked findings. If no finding boxes are selected, Corvus includes the complete finding register.

The investigation is ready to close or hand over only when the exported material accurately reflects both the positive evidence and the limitations encountered during collection.

## 5. Understanding case status

A case can be queued, running, completed, failed, timed out or cancelled depending on its collector state.

The case page shows a collector activity strip and a detailed run section. Each collector run records:

- tool name and version;
- timeout and runtime;
- terminal status;
- rate-limit state where known;
- execution log;
- raw JSON/evidence references;
- error or limitation information.

A collector can complete successfully while still reporting an upstream limitation such as a CAPTCHA. In that situation Corvus can retain any valid evidence already collected while separately telling the analyst that manual action is required.

### Retry one unsuccessful collector

When a case has finished, open **Detailed tool runs**. Failed, timed-out and cancelled
collectors expose a **Retry** action with a timeout input. Adjust it between 10 and
3,600 seconds, then retry only that collector. A new run preserves the original query,
query type and pivot source; the earlier attempt, logs and findings remain available.
The new attempt appears in the normal activity strip. An active case cannot be retried
again, and a previously retried attempt points you to its newer attempt rather than
creating duplicates. Rate limits and manual challenges use their existing operator
workflows instead of automatic retries.

### Compare evidence across cases

On the dashboard, select **2–5 finished cases** and choose **Compare selected cases**.
The comparison identifies retained positive/likely evidence with the same exact URL,
platform account ID, public email, image SHA-256 or published GitHub signing key.
Each shared signal lists its cases, source collectors, observed times, source URLs
and run-evidence links. GitHub public commit identities and profile emails are
included when the collector retained them explicitly.

Use **Export comparison JSON** or **Export evidence CSV** to retain the comparison.
No new collection starts, and unselected cases are excluded. Matching names, similar
usernames, a shared mail provider or a similar image are not sufficient. A shared
image/key/email remains a correlation lead rather than proof of a shared operator.

Comparisons support up to 10,000 retained findings across the selected cases. Up to
500 shared signals are displayed/exported; the page explicitly reports truncation.
Select fewer cases to narrow a larger comparison. On mobile, evidence is shown as
labelled cards so source and case links remain readable.

## 6. Search-engine results and relevance filtering

Search engines frequently return irrelevant material, especially for uncommon usernames, malformed operator queries or engines that ignore part of a query. Corvus therefore does not treat search-engine ranking as evidence by itself.

For automatic username and name searches, Corvus applies relevance gates before a result is retained. A result that does not contain the expected username/name evidence is rejected from the evidence register, although raw search responses remain available for technical review where appropriate.

SearXNG engine choices are passed explicitly to the local SearXNG service. Corvus also records which engines were requested and which upstream engines reported failures or challenges.

## 7. CAPTCHA and manual sock-browser workflow

When an upstream search engine reports a CAPTCHA or challenge, Corvus surfaces a compact, collapsed **manual action required** notice on the investigation page so challenge controls remain available without consuming half the case view.

The notice identifies the challenged engine and the exact blocked query. Choose **Open and clear <engine>**. Corvus opens that same engine in the persistent isolated browser with the harmless warm-up query `time in new york`.

Choose the exact blocked query in Corvus. The same click opens the noVNC browser desktop in a new tab and sends the Corvus tab directly to the held capture controls. Complete the visible CAPTCHA or block manually; Corvus does not attempt to bypass it. **Resume after manual action** continues the current phase. If that was the harmless warm-up, Corvus next runs the original blocked query on the same engine. If the original query itself is challenged, Corvus keeps that exact search open and resumes it in place rather than launching a duplicate.

If the challenge cannot be cleared, use **Abandon** to release the browser slot. The retained capture then offers **Retry exact … search**; ordinary failed or historical captures offer **Run this capture again**, so a specific search never has to be reconstructed manually outside Corvus. The handoff supports the configured browser-search engines, including Bing, Brave, DuckDuckGo, Google, Mojeek, Qwant and Startpage, when the upstream failure indicates a CAPTCHA, challenge, rate limit, access denial or temporary block.

Do not use a personal, work or financial browser profile in the sock browser. Treat the Corvus browser profile as a dedicated investigation identity.

## 8. Managed sock browser

The managed browser exists for workflows that cannot be completed reliably by anonymous HTTP collectors, including login-dependent public pages, search-engine challenges and rendered JavaScript-heavy pages. It is not intended to replace ordinary analyst browsing.

Corvus provides two selectable SOCK engines. **Chromium** uses standard Playwright, or Patchright when deployment stealth mode is enabled. **Firefox** uses Invisible Playwright's patched Firefox engine and is always launched in its engine-level anti-detection mode. The managed-browser page presents both as explicit engine cards rather than a hidden dropdown; the active engine is clearly labelled and switching is blocked only while a capture is active. Identity-profile backup/restore controls are collapsed under advanced browser administration.

Each named SOCK identity has separate Chromium and Firefox persistent profile storage. The Firefox fingerprint seed is deterministic for the identity name, so reopening that Firefox identity does not silently present a different synthetic machine. Supported collector integrations can receive narrowly scoped temporary cookie exports from the currently selected browser. Those exports are created for one collector run and deleted afterward.

Current principles:

- credentials stay in the browser profile;
- cookie material is not written into findings, reports or audit events;
- only matching cookies are shared with supported collectors;
- browser evidence includes bounded HTML, screenshots, hashes, browser engine and version;
- private-address navigation and unsupported target classes are rejected;
- CAPTCHA handling is manual;
- a shown challenge is never solved automatically by either backend;
- Firefox falls back to protocol-level DOM extraction when page CSP blocks the normal JavaScript extractor.

Named browser identity profiles can be used to keep separate operational identities isolated from one another. Profile and engine switching are blocked while a browser capture is active. Browser identity backups are encrypted and password protected and apply to the currently selected browser engine.

Live comparison on Debian on 2026-09-02 used the same five public search flows through both engines. Firefox/Invisible Playwright completed Startpage, DuckDuckGo, Bing, Google and Brave without a challenge. Chromium completed Startpage, DuckDuckGo, Bing and Brave but Google returned its normal `unusual traffic` challenge. Chromium extracted ten Bing result cards where Firefox's CSP-safe fallback retained the page/evidence but found no external Bing result anchors; Firefox extracted ten Google results and 45 Brave external-link candidates. Treat those counts as implementation acceptance observations, not a guarantee about future search-engine behaviour.

## 9. Account evidence and correlation

The account view groups findings that appear to describe the same public account while preserving every source underneath. Depending on the collector, account evidence can include:

- username and display name;
- canonical profile URL;
- account or platform ID;
- biography/about text;
- avatar/profile image;
- follower/following counts;
- account creation dates, source-record update dates and Corvus observation dates;
- declared location;
- verification/private state;
- source-specific metadata.

Corvus may calculate account correlations using shared identifiers or media hashes. A correlation is a lead. It is not an identity determination unless the available evidence supports that conclusion.

Use analyst merge/split controls when automated clustering is clearly wrong. Analyst overrides are audited separately from source evidence.

### Date semantics on account cards

Corvus deliberately separates dates that describe the source account from dates that describe Corvus's own collection activity:

- **Account created** is shown only when the platform, API or bounded extractor exposes a genuine creation/registration field. It can include a full time when the source provides one.
- **Profile record modified** means the source exposes a profile-record update field. It must not be read as proof the user was active at that time.
- **Source data updated** describes a third-party source or tracker refresh and is excluded from the user-activity timeline.
- **Last observed by Corvus** is the newest retained Corvus observation of that account. Where more than one retained observation exists, **First observed by Corvus** is also shown. Neither proves the account was created, modified or actively used at that time.

Twitch is enriched from the bounded public TwitchTracker page when Maigret reports a claimed Twitch profile. Corvus retains the Twitch channel ID and the full **Created** timestamp exposed by TwitchTracker. TwitchTracker's page `Updated` timestamp is retained as **Source data updated** and explicitly excluded from user activity because it represents tracker refresh time.

SoundCloud is enriched from the bounded public `window.__sc_hydration` user record. Useful fields can include account ID, display name, biography, avatar, city/country code, followers/following, track/playlist/repost/comment/like counts and `last_modified`. On live testing on 2026-09-02, SoundCloud returned `created_at: null` for all six tested public profiles, so Corvus does not manufacture a creation date. `last_modified` is presented as **Profile record modified**, not last activity.

Broad username findings are evidence-weighted. A Maigret `Claimed` row with account-specific extracted evidence such as a platform ID, display name, avatar, creation/activity date or account metrics remains a positive lead; a bare site-existence assertion remains a technical candidate. Blackbird follows the same principle. For Roblox, Corvus supplements Blackbird's discarded validation response with Roblox's public exact-username user lookup and retains the returned account ID/name before assigning higher confidence. This restores useful evidence without returning to the older behaviour of treating every broad-checker hit as proven. A Maigret record is also treated as a lead when its only extracted name is the site's own name (for example a page title such as "VK | VK"), or when the account name it reports differs from the one searched. Naminter's `partial` results (only some detection criteria matched) are leads too. If Maigret could not reach 20% or more of its sites because of DNS, connection or timeout errors, the run shows an incomplete-coverage note; accounts on those sites are not ruled out, so check the network or resolver and run it again. The other containerised collectors print nothing when lookups fail, so each tool container also checks at the end of a run whether hostnames still resolve and adds the same note when they do not. A local DNS filter with a per-client rate limit (Pi-hole's default is 1,000 queries a minute) is a common cause: a single broad username run exceeds it.

## 10. Profile validation

The **Validate accounts** action performs a broader analyst-triggered validation pass over candidate profile URLs.

Automatic post-collection validation is intentionally bounded so large cases do not remain apparently stuck while hundreds of profile URLs time out. Manual validation can be used when broader checking is justified.

Validation outcomes may include confirmed, likely, missing, blocked or unresolved. A blocked profile is not the same as a missing profile.

An unresolved result (a redirect, a sign-in or sign-up page, a page that could not be compared) says nothing about the account either way. It keeps a weak lead weak, but it does not lower a finding for which a collector extracted account details.

A page that merely shows the searched handle is not treated as a likely account on that alone, because many sites answer with a normal page that echoes whatever handle is in the address. Before marking such a page likely, Corvus requests the same address for a made-up handle. The page is marked likely only when the site refuses the made-up handle (a missing page, a redirect, or a page that does not show it); when the site serves the same kind of page, or the comparison cannot be made, the result stays unresolved. Cases validated before this check keep their old result until **Validate accounts** is run again; doing so returns a lead the old check had marked likely to a low-confidence lead.

## 11. Search query workbench

Each relevant case includes a **Search query workbench** for deterministic manual search pivots.

The workbench can generate query packs using combinations of:

- exact identifier searches;
- platform/site restrictions;
- file-type operators;
- date ranges;
- domain and identity pivots;
- full-name variants.

Generated searches are analyst tools rather than automatic evidence. Results can be reviewed and selectively imported into the case.

Use the sock-browser action when a search engine challenges automated access or when rendered browser results are materially better than the API/RSS/metasearch path.

### Capture found profiles

Collectors tell you an account exists. The bio, counts, links and avatar are on the profile page itself, and for some platforms only a signed-in browser can read them. On the **Managed browser** page, **Capture found profiles** opens each found profile page in the sock browser for you, one at a time and slowly.

- **What it captures** — profile pages already in the case with status found or likely, on Instagram, Facebook, TikTok, Snapchat or Telegram. Facebook pages addressed by a number rather than a name, and Discord and WhatsApp, are not included; capture those by hand. Only an account's own page counts; posts, videos and listings are left out. A search result is included only when its handle is one of the usernames in the case. A page whose profile the browser has already read is not opened again; a page that was opened but gave nothing stays in the list.
- **Sessions** — it uses whatever this browser identity is signed in to. Public Instagram profile pages are attempted without sign-in; some pages may still present a login wall or challenge. Facebook continues to require browser authentication for its managed profile capture. Corvus never signs in for you.
- **Pacing** — 10 pages by default (30 at most), with a random pause of 30 to 75 seconds between pages. Pages are ordered so that consecutive requests go to different platforms where possible.
- **Platforms the browser cannot read** — if the browser reports that a platform needs a signed-in session, or that its host is not in `OSINT_BROWSER_ALLOWED_HOSTS`, that platform is dropped for the rest of the run after the first page and its pages are marked skipped with the reason. They stay in the list. The built-in hosts always apply and an allowed-hosts value in `.env` only adds to them, so this should arise only for a host you have not added.
- **Accounts that do not exist** — a platform's "couldn't find this account" or "content was not found" page is not read as a profile, even when the platform serves it as a normal page.
- **Sign-in walls and challenges** — never handled automatically. The page is released, that platform is dropped for the rest of the run, and its remaining pages are marked skipped. A platform that twice in a row returns a page with no readable profile is dropped the same way, because that usually means it wants a sign-in. Deal with it in the visible browser and capture those pages with **Collect one approved page**, or start another run afterwards.
- **Outcomes** — *completed* means a profile was read (the **New findings** column stays 0 when the page enriched a finding that already existed). *No profile* means the page loaded but held no readable profile for that account: a sign-in wall the browser did not flag, a removed account, or a page for someone else. Treat it as unresolved and open the capture.
- **One job at a time** — the sock browser has a single capture slot, so this run and the automated dork run cannot be active together on a case. Two failed captures in a row stop the run.
- **Pinterest and Threads** — exact account URLs can be captured and verified directly in the managed browser alongside TikTok, Instagram and Facebook. An unverified exact-handle Instagram or Pinterest lead can be selected for browser verification; the URL is not promoted to a verified account until usable profile data is captured. Public Instagram profiles can now be captured without an authenticated session; Facebook still requires authentication within the active SOCK browser profile. Ordinary browser sessions are not transferable automatically.
- **Cancel run** stops after the page in progress.

A captured profile shows that the account exists and what it publishes. It does not show that the person you are investigating owns it.

### Automated social-platform dork run

On any case that contains a username, full name, email address or phone number, the **Google dork search** page includes **Start automated run**. It covers the primary search value and every additional username, full name, email address or phone number in the case. Usernames and full names take the social-platform searches first. It sends a short set of searches through the sock browser in the background, so you do not open each dork by hand.

A new case starts this run by itself, with the default engines, query limit and pauses, once its collectors have finished. Switch that off under **Settings → Automatic dork run**. If the browser is not ready, or a profile capture run is using it, the automatic run is skipped and the reason is recorded in the audit trail; start it by hand when you are ready.

- **What it searches** — grouped social-platform queries first (Instagram, Threads and Facebook; TikTok, Snapchat and YouTube; X, Reddit and LinkedIn; Pinterest, Twitch, Telegram and Tumblr), then the documented queries from the manual dork pack. Email addresses and phone numbers do not get the grouped social-platform searches; they get the wider pack of documented queries. Grouping several sites into one query keeps the number of requests low.
- **Engines** — DuckDuckGo and Brave are selected by default; both honoured grouped `site:` searches on Chromium in testing, and Brave did on Firefox as well. **Bing**, **Startpage** and **Google** are optional and off by default. In testing Bing ignored `site:` and `filetype:` restrictions and returned the same ten links for three different searches, so it only receives searches with no `site:` or `filetype:` restriction and a run needs at least one other engine selected. Startpage serves results from Google's index but showed a browser check that did not clear, and Google is the engine most likely to challenge.
- **Maximum searches** — 10 by default, up to 30.
- **Minimum / Maximum pause** — each wait between searches is picked at random between the two values (45–90 seconds by default).
- **Results** — a result is imported only if it obeys the search's own restrictions and matches the identifier. A result from a site the search did not ask for, or without the requested file type, is discarded, because some engines ignore those restrictions. For a username, the result must concern that exact handle (the rule is set out below), so `jdoe` does not match `jdoe_fan`, `bigjdoe` or a page that merely mentions the word. These rules also apply to **Search web in sock browser** and to searches resumed after a challenge. Only results that pass are imported, and each search keeps its browser capture as evidence. For an email address or phone number, a result is kept only when its title or snippet contains that address or number; a result with no snippet is kept only when a `site:` or `filetype:` term narrowed the search. The sock browser reads each result's title and snippet from DuckDuckGo, Brave and Startpage, with a structural fallback for any engine whose layout it does not recognise. When a result is an account's own page, the follower, following and post counts and the display name shown in the result are kept as details labelled as coming from the search result, not from the account. The handle in an account-page result is added to the case's username follow-ups; post, search and group URLs are ignored. The table shows how many result links were read and how many were imported. **No results** means no result links could be read from the page; treat it as unresolved and open the capture, because the engine may have served a blocked or unreadable page.
- **Failures** — a search that fails outright, for example because the engine's address did not resolve, is sent once more through a different engine.
- **Browser checks** — some sites show a short "verifying your request" page that a visitor's browser completes by itself. The sock browser waits up to 20 seconds for such a page to clear before capturing, on every capture and not only in this run. It does not click or answer anything; a check that does not clear is treated as a challenge.
- **Challenges** — if an engine shows a CAPTCHA or block, Corvus releases that search, stops using that engine for the rest of the run and sends the search once through another selected engine. Challenges are never solved automatically. The released search stays in the Browser page's capture history, where **Run this capture again** lets you clear it by hand.
- **Cancel run** — stops after the search currently in the browser. Results already imported are kept.

If two searches in a row fail outright, the run stops and reports the error, because that points at the browser or its configuration, not at one engine.

The run uses the active identity and engine. The sock browser handles one capture at a time, so a manual capture started while a search is in flight is refused; try it again a few seconds later, or cancel the run. Deleting a case stops its run and removes the stored queries. A run interrupted by an application restart is marked stopped and can be started again. Expect occasional challenges: pacing and engine rotation reduce them but cannot remove them.

Result-card and snippet reading was confirmed live on 2026-11-13 for DuckDuckGo and Startpage. Brave returned an internal error on both attempts that day and remains unverified. The reading needs a browser-agent container image built from this version; an older image uses the earlier reading.

For a username, a result is kept only when it concerns that exact handle. An account page must be the handle itself, so `instagram.com/linusexample` is not kept for `example`. A post or video on a social site must sit under the handle's own address or @-mention it. A handle that is an ordinary word must lead a segment of the page address or be @-mentioned, because such a word appears in countless titles. The same rule applies to SearXNG and metasearch results. A site that gives each account its own hostname, such as `jdoe.tumblr.com`, counts as that handle's page. Results imported before this rule existed are left as they are; delete them or rerun the search if you want them judged again.

## 12. Historical URL intelligence

Domain investigations may show a unified **Historical URLs** view. This presentation can combine observations from sources such as Common Crawl, AlienVault OTX, URLScan and Wayback metadata.

Dates shown in this section are source observation dates. They do not necessarily prove publication date, first appearance or current availability.

Underlying source findings remain unchanged and separately attributable.

## 13. Live Site Intelligence

Live Site Intelligence is a bounded same-domain crawler intended to collect public, directly observable information without active exploitation.

It is GET-only, follows conservative domain boundaries, honours robots controls and limits traversal depth, page count, JavaScript retrieval and total response volume.

It can retain public items such as:

- email addresses;
- document links;
- social links;
- external-domain references;
- conservative JavaScript endpoint strings;
- declared technology hints;
- tracking identifiers.

Tracking identifiers are correlation leads only. Shared analytics IDs do not by themselves prove common ownership.

## 14. Registration and infrastructure enrichment

Domain Registration uses standards-based RDAP rather than legacy WHOIS scraping. Corvus uses IANA bootstrap data to locate the authoritative RDAP service and retains bounded public registration information.

Other optional infrastructure enrichments may accept public IP addresses or ASNs. Private/non-routable targets are rejected from public-network enrichment paths.

## 15. Telegram and Discord evidence imports

Corvus supports local analysis of analyst-supplied export files for selected platforms.

### Telegram

Telegram Desktop JSON exports can be imported locally. Parsed messages retain timestamps, sender details, reply/reference information, public identifiers, URLs and attachment references within configured limits.

### Discord

DiscordChatExporter JSON and supported discrawl-compatible SQLite archives can be imported without contacting Discord. Imported Discord authors can become first-class profile findings, and guild/channel/message relationships enter the normal Investigation Graph.

Use lawful export methods and appropriate platform credentials. Do not use selfbots or harvested user tokens.

## 16. Investigation Graph

The graph provides a visual relationship layer over evidence already retained in the case. Nodes can represent identifiers, accounts, URLs, domains, locations and imported platform entities.

Edges retain relationship type and provenance. Analyst-created relationships and notes are stored separately from source-generated relationships.

Use the graph to organise evidence and identify pivots; do not infer causation or identity merely because two nodes are connected by a weak shared attribute.

## 17. Pivots

Corvus can identify candidate pivots from existing evidence, such as:

- username candidates from names or email addresses;
- email addresses exposed in public profiles;
- display names;
- domains and URLs;
- location or organisation identifiers.

Automatic pivoting is deliberately restricted. Manual pivot actions show the compatible collector set before execution and enforce maximum pivot depth to prevent recursive case explosion.

## 18. Raw evidence and provenance

Every important automated collector should retain enough information to understand how a finding was produced.

Depending on the source, this can include:

- raw JSON;
- retained stdout/stderr execution log;
- source URL;
- query used;
- engine/source attribution;
- collector version or pinned commit;
- browser screenshot and HTML hash;
- observation timestamp.

The normalised finding is the convenient analytical layer. Raw evidence is the technical source layer.

## 19. Reports and exports

Corvus supports case exports intended to preserve provenance while providing a readable analytical view.

Available outputs include structured JSON/CSV and a branded HTML/print report. Evidence bundles can include selected findings and bounded retained artifacts with SHA-256 manifests. In the analytical report, **Investigator notes** appear immediately after the executive overview so examiner context is read before unified-account and detailed finding sections. Platform cards use bundled local marks where available and a deterministic local fallback mark otherwise; exported reports never fetch live favicons.

Before sharing a report:

1. verify the case title/reference;
2. review likely/unknown findings;
3. remove or annotate obvious false positives;
4. check that conclusions distinguish observation from attribution;
5. confirm the intended redaction options;
6. preserve the corresponding raw evidence where evidential continuity matters.

## 20. Rate limits, blocks and service failures

External services change frequently. A collector can stop working because of rate limits, CAPTCHA, endpoint changes, geographic behaviour or upstream outages even when Corvus itself is healthy.

Treat these states distinctly:

- **rate limited** — upstream explicitly or strongly indicates throttling;
- **manual action required** — a CAPTCHA/challenge requires analyst interaction;
- **blocked/access denied** — the source refused automated access;
- **timed out** — the configured bounded runtime expired;
- **failed** — the collector could not complete its contract;
- **completed with zero findings** — no relevant result was retained; this is not automatically proof of absence.

Review the execution log and raw response before deciding whether to rerun a collector.

## 21. Settings and toggle reference

Settings contains deployment-wide defaults and credentials. Most changes apply to **new** work only; existing queued, running and completed runs retain the values recorded when they started. A case-level timeout or collector selection can override the corresponding default for that investigation.

Avoid enabling every possible source merely to maximise result count. More sources can increase noise, latency and rate-limit exposure without improving evidential quality.

### Per-collector timeout defaults

Every collector has a numeric default timeout. The allowed range is shown in the UI.

- Increasing a timeout gives a slow source more time to finish but can make cases take materially longer.
- Reducing a timeout bounds slow collectors more aggressively and may increase timed-out or unresolved runs.
- A timeout affects only that collector; other collectors continue independently.
- **Save timeout defaults** stores the displayed values for new work.
- **Reset built-in defaults** restores the built-in collector values.

### SearXNG engines

These checkboxes control the locally hosted engines used by the SearXNG name/username collector.

- Checked = requested on new compatible SearXNG runs.
- Unchecked = excluded.
- **Unavailable** means the engine is not present in the deployed local SearXNG configuration and cannot be enabled from Corvus.
- **Save engine selection** stores the selected set.
- **Reset safe defaults** restores the recommended keyless selection.

Engine selection is retained in raw/query metadata for provenance. One engine failing, rate-limiting or challenging the request does not automatically fail the whole collector.

### Metasearch2 engines

These checkboxes choose the engines queried by the optional Metasearch2 web-discovery collector.

- Checked = included in new Metasearch2 requests.
- Unchecked = excluded.
- **Save engine selection** stores the chosen set.
- **Reset recommended defaults** returns to the recommended engine set.

Metasearch2 itself is not selected by default for new investigations. These switches only define what it may use when selected.

### Phone-presence platforms

These switches control which platform checks are permitted inside the manual **Phone Number Presence** collector.

- **Snapchat** — enabled by default.
- **Instagram** — off by default; higher-risk and more prone to platform blocking.
- **Amazon** — off by default; similarly higher-risk.
- **Save platform selection** stores the allowed set.
- **Reset to Snapchat only** restores the conservative default.

This setting does not automatically run phone-presence collection. The collector remains manual/non-default per investigation.

### Runtime option: Share authenticated browser sessions

When enabled, supported collectors may receive a narrowly scoped temporary cookie export from the active sock-browser identity.

- Instaloader receives Instagram-relevant cookies only. It no longer invokes Instaloader's legacy `test_login()` GraphQL endpoint (`d6f4427fbe92d846298cf93df0b937d3`): Instagram may reject that endpoint with `feedback_required` even when the browser is signed in. Cookie transfer does **not** by itself prove API authentication; the actual profile lookup is attempted with a bounded request, and any refusal remains an API-only limitation. Without a signed-in Instagram session in the sock browser, the Instaloader API lookup is skipped because Instagram rejects unauthenticated API requests. This does **not** prevent the separate public-profile capture in Corvus's managed browser.
- **Public Instagram browser collection** — when the selected Instaloader API lookup cannot run without authentication or its API response is inconclusive, Corvus attempts the public profile using its existing SOCK browser even if the browser has no Instagram login. This is a separate browser observation, not an Instaloader API result. A successful capture retains a screenshot/manifest and enriches the case with rendered profile metadata, including available follower/post counts. If the page shows a challenge, Corvus records it without signing in or solving it; no access is falsely inferred from a login wall. A busy browser capture may be retried after initial collectors complete. Browser capture status is audited. An operator may always use **Capture through sock browser** directly.
- Maigret receives cookies only for its supported social-site scope.
- Other collectors remain anonymous unless explicitly integrated.
- Temporary cookie copies exist only for the collector run and are deleted afterward.

Turn this off when you deliberately want supported collectors to remain anonymous even though the sock browser is logged in.

### Runtime option: Automatic Wayback lookup

When enabled, Corvus checks retained public profile URLs against the Internet Archive after collection finishes.

- Archive observations are attached to the relevant finding/account view.
- Archive dates are not treated as account-creation dates.
- Turning this off prevents the automatic post-collection lookup; the manual Wayback workspace remains available.

### Runtime option: Route phone-presence checks through Tor

When enabled, Snapchat/Amazon phone-presence checks use the configured local Tor sidecar instead of the deployment's normal egress IP.

This is an **egress-isolation** control, not a reliability or anonymity guarantee. Tor exits may be blocked or scrutinised, and the switch has no effect when no Tor proxy is configured.

### Steam Web API

- **Save Steam API key** stores the key locally and enables the native Steam Public Profile collector.
- **Clear configured key** removes it and disables the collector until another key is supplied.

The existing key is never displayed back in the browser and is not copied into case evidence or audit detail.

### WhatsMyName coverage dataset

This section reports the independently refreshed WhatsMyName dataset revision, retrieval time, site/category counts, protected-site count, schema issues and SHA-256.

- **Refresh coverage dataset** downloads and validates the current dataset, stores it locally with restrictive permissions and records the refresh in the audit log.
- This coverage dataset is for inspection/coverage analysis and does not silently replace Blackbird's release-pinned runtime data.

### Companies House API

A configured Companies House API key enables the native, manual-only UK company/officer collector for Name investigations. Corvus bounds company/officer search results and appointment expansion, keeps the key local, and does not echo it into evidence or audit details. The Irish CRO Open Data collector needs no key and remains manually selectable for Name cases.

### AbuseIPDB API

A configured AbuseIPDB API key adds **AbuseIPDB · 90-day reputation** to the explicit infrastructure enrichment selector for public IP addresses. Corvus uses the API `CHECK` operation only, does not submit reports and does not request verbose reporter comments.

### OpenPhish authorised feed

OpenPhish correlation is disabled until the operator stores an authorised OpenPhish-hosted feed URL supplied under the organisation's licence/Gov access. Corvus does not enable the public Community feed by default because its terms restrict organisational operational use. The configured feed URL is treated as a secret; case evidence retains only matching URLs plus feed hash/retrieval provenance, not the access URL or full feed.

### URLScan API

URLScan is a specialist **manual-only domain collector** using the historical Search API. Corvus does not submit new URLScan scans.

- **Save URLScan key** stores the API key locally and makes URLScan available among compatible domain collectors.
- **Clear key** removes it and disables URLScan collection.
- Even when configured, URLScan remains unchecked by default on new cases.

### Investigation-start controls

The dashboard also contains case-specific controls that are not global Settings values:

- **Query type** — Username, Name, Email, Phone or Domain. Auto-detection is advisory; the operator can override it.
- **Investigation title** — optional display title.
- **Case reference** — optional external reference.
- **Collector checkbox** — checked collectors run; unchecked collectors are deliberately skipped.
- **Per-case timeout** — overrides the Settings default for that collector in this case only.
- **Proxy** — Disabled, HTTP, HTTPS, SOCKS or Tor using deployment-configured proxy values.
- **Tags** — comma-separated local case labels.
- **Automatically follow corroborated username/email identity pivots** — username and email cases; checked by default. Runs a bounded two-hop expansion of corroborated usernames and independently observed public emails.
- **Extended username search** — username cases only. Adds the Maigret all-sites and Naminter/WhatsMyName sweep and increases runtime/noise.

### Investigation-page display controls

- **Finding status filter** — defaults to Positive only; switch to All statuses or a specific state for completeness review.
- **Finding text filter** — filters visible findings by platform/tool text.
- **Group by platform** — groups finding cards by platform when checked; uncheck for an ungrouped record view.
- **Automatically follow future corroborated identity pivots** — case-level toggle in the pivots section, applied with **Save setting**. Changes future follow-up behaviour for that investigation only.

### Correlation and enrichment thresholds

- **Respect robots.txt** — enabled by default for public-profile enrichment and location collection. Disabling it is audited.
- **Image distance threshold** — perceptual dHash distance for image-correlation candidates. Lower values are stricter; `0` requires identical perceptual hashes.
- **Face minimum cosine similarity** — minimum local SFace similarity for surfacing a candidate. It is not an identity probability.
- **Account minimum score** — minimum transparent multi-signal correlation score shown to the operator.
- **RIPEstat / ARIN RDAP / AbuseIPDB selector** — chooses the explicit infrastructure source. AbuseIPDB appears only when its API key is configured and accepts public IP addresses only.
- **VAT number** — the VIES action requires the country prefix; Greek `GR` input is normalised to VIES `EL`.
- **Public video URL** — yt-dlp metadata extraction is limited to supported YouTube, Vimeo, X/Twitter, Reddit and Facebook URLs and is always metadata-only/no-playlist.

### Search query workbench controls

- **Search engines** — choose which generated query cards to build.
- **From date / To date** — bound date-aware search queries where supported.
- **Include exact-match and identity/domain pivot queries** — includes the core deterministic query pack.
- **Include deterministic name variants and transliterations** — name cases only; generates local, rule-based variants.
- **Known alternate names or aliases** — operator-confirmed aliases, one per line. Corvus does not invent culturally specific nicknames.
- **Document types** — adds selected file-type query variants.
- **Platform selections** — adds site/platform-specific query variants.
- **Reusable templates** — includes saved custom query templates.
- **Open search** — opens the signed generated search normally.
- **Search with sock browser** — opens the generated search through the managed browser.
- **Search via SearXNG** — executes the generated query through the local SearXNG path.
- **Originating query** on result import — records which generated query produced the analyst-selected result.
- **Import and deduplicate** — adds selected search evidence while avoiding duplicate stored records.

### Managed-browser controls

- **Chromium / Firefox** — selects the active capture engine. Switching is blocked while a browser capture is active.
- **Profile name / Activate identity** — selects or creates a named browser identity. Chromium and Firefox maintain separate persistent storage under the same identity name.
- **Download encrypted backup** — exports the selected browser identity using the supplied backup password.
- **Restore encrypted backup** — restores a `.cbb` identity backup under the chosen profile name.
- **Capture through sock browser** — manually captures one approved public HTTPS URL into the case.
- **Resume after manual action** — resumes a held CAPTCHA/login-wall capture after the operator handles it.
- **Abandon / Mark abandoned** — releases a held capture without treating it as successfully completed.
- **Retry exact <engine> search / Run this capture again** — reruns the specific historical search/capture rather than reconstructing it manually.

### Investigation Graph controls

The graph is a presentation and analysis layer over retained case evidence. Visibility/layout controls do not delete evidence.

- **Find an entity** — highlights matching nodes; non-matching entities are dimmed rather than removed.
- **Layout** — chooses Subject centred, Group by type, Provenance flow or Chronological timeline. Applying Chronological timeline also switches the **Timeline events** filter on.
- **Apply layout** — rearranges currently visible nodes using the selected layout.
- **Fit graph** — zooms/pans to fit all visible nodes.
- **Include selected only** — temporarily shows only the selected node and its directly connected evidence.
- **Exclude selected** — hides the selected node from the current graph view only.
- **Clear include-only** — leaves the one-hop include-only view and restores current filter-based visibility.
- **Restore excluded** — restores nodes manually hidden during the current graph session.
- **All findings** — reveals low-confidence and single-source findings normally hidden from the concise graph view.
- **Candidate relationships** — shows proposed or weakly supported relationships not accepted as identity links.
- **Contradicted findings** — shows evidence contradicted by validation, such as a confirmed-missing account.
- **Collector provenance** — displays collector nodes and `reported-by` relationships.
- **Images** — displays retained profile-image evidence and image-correlation relationships.
- **Locations** — displays location observations; enabled by default. A location node is evidence, not proof of residence.
- **Archives** — displays Internet Archive observations and archive relationships.
- **Timeline events** — displays parsed account creation, last-online and public-activity events with source-field provenance.
- **Save layout** — persists node positions for this investigation. Visibility filters and temporary exclusions are not saved.
- **Reset layout** — removes saved positions and returns to the default arrangement.
- **Export PNG** — exports the visible graph canvas; profile photographs are omitted from the bitmap.
- **Export graph JSON** — exports the complete graph evidence projection and saved/current positions; temporary visibility filters do not limit the JSON export.
- **Node note** — adds an analyst note to a selected graph entity.
- **Manual relationship** — creates an analyst-defined relationship between two nodes with a label and optional note.
- **Remove relationship** — deletes an analyst-created relationship only; source-generated evidence remains intact.

### Wayback / identity-evolution controls

- **Submit public capture** — explicitly asks the Internet Archive to save a public URL. Use only when appropriate; this is an outbound action and differs from passive archive lookup.
- **From year / To year** — bounds capture discovery by calendar year.
- **Include all archived paths under a bare domain** — expands a bare-domain query to archived paths beneath that domain.
- **Treat an HTTP(S) URL as a prefix and include archived descendants** — expands URL discovery to descendant paths under the supplied prefix.
- **Discover captures** — retrieves matching archive observations using the selected scope.
- **Compare first/latest** — fetches only the earliest and latest distinct HTML captures for a URL and compares bounded titles, descriptions, emails, links and image references. Archived scripts are not executed.
- **Build identity evolution** — builds a dated view across the discovered captures for the selected URL.

### Location-intelligence controls

- **Respect robots.txt** — honours source robots rules during supported collection.
- **Collect this page** — collects one supported public source URL entered by the analyst.
- **Collect supported finding pages** — runs supported location collection across candidate URLs already retained in the case.
- **Place or venue** — analyst-supplied place label.
- **Evidence type** — classifies the type of location evidence and determines the default confidence model.
- **Source platform / Public source URL** — records provenance.
- **Occurred / claimed visit date** — when the underlying event/visit is claimed to have happened.
- **Published date** — when the source published the evidence.
- **Locality / Region / Country** — structured place description.
- **Account identifier** — links the observation to an account/identifier where known.
- **Latitude / Longitude** — optional coordinates.
- **Precision radius** — uncertainty/precision in metres.
- **Confidence override** — optional explicit 0–1 analyst confidence instead of the evidence-type default.
- **Source platform hides or approximates the precise location** — marks privacy-masked/approximate source geography so Corvus does not overstate precision.
- **Add observation** — stores the analyst-entered location observation with its provenance and confidence fields.

### Evidence-bundle controls

- **Include bounded raw case artifacts** — includes retained raw artifacts subject to bundle limits.
- **Redact identifiers** — applies supported identifier redaction in the portable bundle.
- **Redact notes** — excludes investigator notes.
- **Redact external URLs** — removes external URLs where supported.
- **Select individual findings** — restricts the bundle to checked findings. With none selected, the complete finding register is included.

## 22. Updates and What's New

Use **Corvus Update** from Corvus or the installed platform launcher.

The updater is designed to:

- fetch updates from the public Corvus GitHub repository by default;
- let you see and change its source: on Windows the **Update source** box in the Corvus Update window (with **Reset to default**), elsewhere `python Update-Corvus.py --show-source` and `python Update-Corvus.py --repository-url <address>`. A supplied address is saved for later updates; a check (`--check --repository-url <address>`) only tests the address and does not change it. Change it only if the source has moved;
- refuse unsafe dirty-tree updates;
- fast-forward only;
- back up the case database before rebuilding;
- rebuild the Docker stack;
- require health checks to pass before considering the update successful;
- refresh the single Windows Corvus Desktop/Start Menu shortcut from the active `.env` after port repair, keeping the Corvus icon and removing obsolete SOCK-browser shortcuts.

The **Corvus Update** page in the app offers **Run Corvus Update** for Windows, plus **Download repair launcher** as a recovery fallback for older or damaged installations where the one-click handler is not registered. macOS uses **Download macOS update launcher**, a reviewable `.command` script. Each of these starts the updater already installed on the machine.

If an update fails, the previously installed build is retained and the updater records the container images needed to roll back. On Windows run `.\OSINTWorkbench.ps1 -Command rollback` from the installation directory; on macOS/Linux run `python3 scripts/installer.py rollback` from the checkout. Image rollback does not reverse database migrations or restore the source tree, so restore the matching database backup when required. The portable updater package is described in `docs/UPDATER.md`.

On Windows, the normal one-click action uses the `corvus-update://` protocol and opens the WPF **Corvus Update** application. It presents a branded status card, progress state and collapsible technical log while reusing the same health-gated update engine as scheduled updates. The old `osint-workbench-update://` protocol remains only as an upgrade-compatibility alias. Scheduled updates remain headless.

The **What's new** page uses tracked build-range metadata to show user-visible changes introduced between installed builds. Use **View full changelog** underneath that summary to open the complete curated changelog shipped with the running build. A stale or empty What's New page when the code has visibly changed is a release-bookkeeping defect and should be corrected before distribution.

## 23. Backups and retained state

Important persistent state includes:

- the Corvus SQLite database and raw collector output;
- managed-browser profile data;
- managed-browser retained evidence;
- installer/update state.

The Docker volumes retaining investigation and browser state may keep legacy physical names for upgrade compatibility even though the current application is branded Corvus. Do not delete those volumes simply because their names predate the rebrand.

Back up required state before Docker Desktop reset, host migration or destructive troubleshooting.

## 24. Troubleshooting: symptom → likely cause → action

Start with the narrowest fault. A collector problem is not necessarily a Corvus problem, and a challenged upstream source is not a negative finding.

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Investigation appears stuck | One or more collectors are still running, or Corvus is in automatic follow-up/verification | Check **Activity** and **Detailed tool runs**. Compare runtime with the recorded timeout. Let independent collectors finish unless there is a reason to cancel. |
| One collector failed but others completed | Upstream/tool-specific failure | Open that run's **Execution log** and **Raw JSON**. Review the error state; rerun/follow up only that source where practical. |
| Collector timed out | Source is slow, blocked or the timeout is too low | Check the execution log. Increase that collector's timeout for a future run only if the source is legitimately slow; do not simply maximise all timeouts. |
| Zero findings returned | Genuine zero result, relevance filtering, block/rate limit or source behaviour change | Inspect run status and raw output. `completed with zero findings` is not automatically proof of absence. |
| Expected search result is missing | Relevance gate rejected it, engine did not return it, or engine was challenged | Inspect SearXNG/Bing run metadata and raw results. Use the Search query workbench or sock browser for manual review. |
| Search engine shows `manual action required` | CAPTCHA, challenge, rate limit or temporary access block | Use **Open and clear <engine>**, solve the visible challenge manually, then **Resume after manual action**. If necessary **Abandon** and **Retry exact <engine> search**. |
| Sock browser opens the wrong/old identity | Wrong named profile or engine is active | On the Browser page confirm the active **Chromium/Firefox** engine and active identity profile. Switch only when no capture is active. |
| Cannot switch Chromium/Firefox | A capture is active/held | Finish, resume or abandon the active capture first. |
| Instagram result lacks HD image/business metadata | The public page omits the field or Instaloader's authenticated API is unavailable | Use the SOCK browser to capture the public profile as evidence; API-only fields may still require an authenticated session. |
| Browser login appears lost | Different identity/engine selected, profile reset, or site invalidated the session | Confirm engine/profile, open the browser desktop, sign in again if required. Chromium and Firefox stores are separate. |
| Browser capture says target not allowed | URL is private/local, unsupported, malformed or outside allowed capture policy | Use a public HTTPS URL supported by the browser collector. Do not attempt to bypass target restrictions. |
| Account is shown `unknown` or `likely` rather than confirmed | Broad existence collector lacks corroborating evidence | Review profile fields and sources, then use **Validate accounts** or supported enrichment. Do not manually inflate confidence without evidence. |
| Account appears missing but source was blocked | Validation could not reach/interpret the source | Treat as unresolved/blocked rather than absent. Check validation detail and retry only when justified. |
| Too many noisy username hits | Extended/broad collectors or weak existence-only results | Keep **Extended username search** off unless needed, filter to positive evidence, review source strength and use validation/correlation rather than raw hit count. |
| A known account (for example Roblox) has low confidence | Only a broad checker found it or supplemental exact evidence was unavailable | Review parsed/raw record and validation. Corvus may enrich exact identifiers where supported; otherwise treat it as a lead. |
| Twitch/SoundCloud dates look odd | Source date represents tracker refresh/profile modification, not account activity | Read the field label. **Source data updated**, **Profile record modified**, **Account created** and **Last observed by Corvus** have different semantics. |
| Profile image is not displayed | Image was not retained, remote fetch failed or source supplied none | Check the image state on the finding. Use supported Load/Open actions or rerun relevant enrichment; inspect image-cache error if present. |
| Image/face/account correlation produces too many candidates | Threshold is too permissive | Lower dHash distance, raise face cosine similarity or raise account minimum score. Review candidates manually. |
| Correlation missed an obvious relationship | Threshold too strict or source fields/images differ | Adjust threshold carefully and rerun correlation. Analyst **Force merge** can record a reviewed grouping without altering source evidence. |
| Wrong findings grouped together | Automated clustering over-correlated them | Use **Force split** or pair-level **Reject/Reset**, and add a cluster note explaining the analyst decision. |
| Search query workbench generates no queries | No engine or query/category option selected | Enable at least one search engine and one of core queries, platform, document type or reusable templates. |
| `Search with sock browser` is disabled | Managed browser unavailable/disabled | Check Browser page/service health. Use normal signed search/SearXNG until the browser service is restored. |
| Automatic Wayback data is absent | Setting disabled, URL not archiveable, no capture exists or archive request failed | Check **Automatic Wayback lookup** in Settings and the Wayback view. A missing capture is not evidence the account/page did not exist. |
| URLScan collector is missing | API key not configured | Configure **URLScan API** in Settings. It remains manual and unchecked by default even when available. |
| Steam collector is unavailable | Steam Web API key missing/cleared | Configure the key in Settings. |
| Phone-presence result fails or rate-limits | Technique is sensitive to platform controls; selected platform may block normal or Tor egress | Review the platform-specific result. Try another permitted egress only when justified; Tor is isolation, not a bypass guarantee. |
| Phone-presence Tor toggle does nothing | No Tor proxy/sidecar configured | Settings shows when Tor is unavailable. Disable the toggle or have the deployment configured appropriately. |
| Location looks more precise than the source really is | Privacy-masked/approximate source not marked or precision radius omitted | Mark **Source platform hides or approximates the precise location** and record an appropriate precision radius. |
| Report contains an obvious false positive | Finding was retained as a lead but analyst review has not contextualised it | Add notes, review validation/correlation and ensure conclusions distinguish observation from attribution. Preserve the source record if evidential continuity matters. |
| Report/CSV/JSON seems to omit a finding | Filtered/selected export, deduplication or bundle-selection setting | Check the full case findings and export controls. In portable bundles, selecting individual findings restricts the bundle; selecting none includes all. |
| Portable bundle contains sensitive material | Redaction options not enabled | Use **Redact identifiers**, **Redact notes**, and/or **Redact external URLs** before creating the bundle. Review the exported result. |
| What's New is blank/stale after a real update | Release build-range/changelog bookkeeping issue | Confirm the running build and full changelog. Report it as a release defect rather than assuming the update failed. |
| Corvus shortcut opens the wrong port or fails | Port changed, stale shortcut or application stack not healthy | Use the current Corvus shortcut/update repair path. Confirm the configured loopback port and application health before manually editing shortcuts. |
| Corvus itself will not load | Docker service failure, port conflict or unhealthy application service | Check Docker Desktop is running, then check Compose/service health and logs. Confirm loopback ports are available. |
| Browser page says managed browser disabled/unavailable | Browser-agent/desktop service unhealthy | Check Corvus service health/update status. The managed browser is part of the standard stack. |
| Update refuses to proceed | Dirty checkout, source/update safety check or health failure | Read the updater technical log. Do not delete local state blindly; resolve the stated condition or use the supported repair path. |
| Case cannot be deleted | Investigation is queued/running or confirmation missing | Cancel active collection first. For bulk deletion use finished cases and the required confirmation text; for case deletion type `DELETE`. |

For any unexplained collector result, use this diagnostic order: **run status → execution log → raw JSON → source limitation → relevance/validation state → browser/manual follow-up**. This keeps technical source behaviour separate from analytical conclusions.

## 25. Evidence-quality rules of thumb

- One matching username is not an attribution.
- One search-engine result is not corroboration.
- A missing result is not proof of absence when the upstream source was blocked or challenged.
- A source observation date is not automatically a publication date.
- Shared tracking IDs, avatars or display names are useful leads but need context.
- Preserve contradictory evidence rather than forcing it into a preferred theory.
- Record analyst decisions separately from collector output.

## 26. Build and documentation rule

`docs/MANUAL.md` is the authoritative operator manual and is part of the Corvus release payload. User-visible workflow changes should update this file in the same release as the code change.

See the README, architecture and operations documentation for installation and implementation details.

---

**Corvus — Operator Manual**

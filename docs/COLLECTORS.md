# Collector catalogue

[Documentation index](INDEX.md)

The catalogue covers registered adapters in the public source, including those registered conditionally through Settings. Availability depends on installed services, credentials and upstream responses. Native online adapters send the selected query to their respective services; container collectors contact their upstream sites. Offline actions do not require new external requests. See [external integrations](UPSTREAMS.md) for pinned revisions and licence details.

## Username and account discovery

| Collector (source ID) | Input | Output | Requirements / limitations |
| --- | --- | --- | --- |
| Maigret (`maigret`) | Username | Site results and profile fields | Container; extended all-sites search optional; MIT |
| Sherlock (`sherlock`) | Username | Site account results | Container; DNS failures reduce coverage; MIT |
| Blackbird (`blackbird`) | Username, email | Account candidates and metadata | Separately obtained compatible runtime; no redistribution permission identified |
| Naminter (`naminter`) | Username | WhatsMyName site candidates | Optional container; pinned CC-BY-SA dataset |
| Nexfil (`nexfil`) | Username | Candidate URLs | Container; independent verification required; MIT |
| Social Analyzer (`social_analyzer`) | Username | Candidate profiles and metadata | Container; bounded results; AGPL |
| Instaloader (`instaloader`) | Username | Instagram profile metadata | Container; API access may require scoped session; separate browser fallback; MIT |
| gallery-dl (`gallery_dl`) | Username | Limited Instagram/TikTok metadata | Optional container; no media download; service access dependent |
| Public Username Profiles (`username_public_profiles`) | Username | Exact public account metadata | Native online lookup across supported platforms |
| Snapchat Public Profile (`snapchat_public`) | Username | Public profile observations | Native online; limited fields vary by profile |
| Telegram Public Page (`telegram_public`) | Username | Public profile/channel observations | Native online; not private chat collection |
| TikTok Public Profile (`tiktok_public`) | Username | Public profile metadata | Native online; upstream access may be restricted |
| Steam Public Profile (`steam_public`) | Username | Public Steam profile metadata | Configured Steam Web API key |
| GitHub Developer Identity (`github_developer_identity`) | Username | Public repository and signing-key correlation leads | Optional native online; not proof of ownership |
| GitHub Public Discovery (`github_discovery`) | Username, name, email, domain | Public GitHub search evidence | Optional native online; rate limits and relevance constraints |

## Name and web discovery

| Collector (source ID) | Input | Output | Requirements / limitations |
| --- | --- | --- | --- |
| Bing Name Search (`bing_name_search`) | Name | Relevance-filtered search results | Native online RSS; limited coverage |
| SearXNG Web Search (`searxng_name_search`) | Name, username | Search results and engine provenance | Local SearXNG service; AGPL |
| Metasearch2 Web Search (`metasearch2`) | Name, username | Bounded web discovery | Optional local service; CC0 |
| DuckDuckGo Instant Answer (`duckduckgo_instant`) | Name, domain | Public instant-answer metadata | Optional native online; not exhaustive web search |
| OpenAlex Scholarly Identity (`openalex_scholar`) | Name | Scholarly identity, affiliations and research metadata | Optional native online; ambiguous names need review |
| GLEIF LEI Search (`gleif`) | Name, domain | Legal entity records | Optional native online; registry scope only |
| Irish CRO Open Data (`cro_open_data`) | Name | Company snapshot records | Optional native online; CC-BY-4.0 dataset; no inferred directors |
| UK Companies House (`companies_house`) | Name | Company/officer records | Optional configured API key; bounded searches |

## Email and phone

| Collector (source ID) | Input | Output | Requirements / limitations |
| --- | --- | --- | --- |
| Holehe (`holehe`) | Email | Account-presence leads | Individually selected container; recovery interfaces may be intrusive or rate limited; GPL |
| Gravatar Public Profile (`gravatar_public`) | Email | Public directory profile | Native online; not proof of identity |
| OpenPGP Public Key (`pgp_public`) | Email | Public key directory observations | Native online; key associations require verification |
| Federated Identity Resolver (`federated_identity`) | Email-form identifier | Public federation discovery metadata | Optional native online; exact compatible identifiers only |
| Phone Number (offline) (`phone_offline`) | Phone | Local number information | Offline; does not identify a subscriber |
| Phone Number Presence (`phone_presence`) | Phone | Platform-presence leads | Optional native online; explicit platform selection; inconclusive responses are not negatives |

## Domain and infrastructure

| Collector (source ID) | Input | Output | Requirements / limitations |
| --- | --- | --- | --- |
| theHarvester (`theharvester`) | Domain | Public domain-discovery records | Container; bounded source selection; GPL |
| Subfinder (`subfinder`) | Domain | Passive subdomains and source references | Container; bounded in-domain output; MIT |
| Common Crawl (`commoncrawl`) | Domain | Historical URL observations | Native online; archive coverage varies |
| Domain Registration (RDAP) (`domain_rdap`) | Domain | Public registration metadata | Optional native online; authoritative service availability |
| Certificate & Email Relationships (`domain_relationships`) | Domain | Certificate and mail-related DNS observations | Optional native online; shared infrastructure is not ownership |
| Microsoft Entra Tenant Metadata (`entra_tenant_metadata`) | Domain | Public tenant discovery metadata | Optional native online; no authentication attempt |
| Live Site Intelligence (`site_intelligence`) | Domain | Source-attributed public links and metadata | Optional bounded same-domain GET retrieval; respects robots.txt |
| AlienVault OTX Historical URLs (`otx_historical`) | Domain | Passive historical URL observations | Optional native online; bounded results |
| URLScan Historical URLs (`urlscan_historical`) | Domain | Historical search observations | Optional configured API key; no new scan submissions |
| OpenPhish Authorised Feed (`openphish`) | Domain | Matches against configured feed | Optional authorised feed; no default community feed |

## Explicit actions and other integrations

These are not ordinary case-start collectors.

| Action | Input | Result / requirements |
| --- | --- | --- |
| yt-dlp video metadata (`yt_dlp_metadata`) | Supported public video URL | Metadata-only container action; upstream restrictions apply |
| TikTok evidence | Individual public video | Bounded acquisition and dedicated evidence ZIP |
| VIES | VAT number | Official EU/NI validation response |
| RIPEstat / ARIN | Public IP or ASN | Public infrastructure metadata |
| AbuseIPDB | Public IP | Optional API-key-backed reputation observation |
| Wayback | Public URL | Historical capture metadata and comparison |
| Telegram / Discord import | Supported local export | Offline source-backed records |
| Retained artifact analysis | Existing case files | Local hashes and metadata |
| Auto Archiver handoff | Selected finding URL | Local handoff bundle; no automatic execution |

GHunt is documented as deferred, not a currently selectable collector. Photon, DataSploit and SpiderFoot are technique references rather than bundled engines. Refer to [UPSTREAMS.md](UPSTREAMS.md) for redistribution conditions and implementation notes.

Collector results are observations or leads. Verify provenance and applicability; do not treat automated correlation as an identity determination.

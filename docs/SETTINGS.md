# Settings and runtime options

[Documentation index](INDEX.md)

Settings control deployment-wide defaults; case controls can override applicable options. Only configure integrations you are authorised to use.

| Setting | Effect |
| --- | --- |
| Collector timeouts | Limits individual run duration; supported range 10–3600 seconds. |
| SearXNG / Metasearch2 engines | Select available local search engines; Metasearch2 is optional. |
| Phone-presence platforms | Manual collector; Snapchat default within that collector, other platform checks opt-in. |
| Session sharing | Temporary scoped exports from the selected managed browser profile to supported collectors. |
| Automatic Wayback | Post-collection archive lookup; manual workspace remains available when off. |
| Phone egress isolation | Optional configured Tor sidecar; no anonymity or availability guarantee. |
| Steam Web API | Enables Steam adapter when a key is configured. |
| Companies House | Enables optional UK registry adapter when configured. |
| URLScan | Enables manual historical search; no scan submission. |
| AbuseIPDB | Optional explicit IP reputation lookup. |
| OpenPhish | Requires authorised feed URL; disabled without configuration. |
| WhatsMyName coverage | Refreshes the separately inspected dataset; does not silently replace pinned Blackbird runtime data. |

Keep secrets local and do not paste settings, browser cookies or raw case logs into public issues. The [complete settings reference](MANUAL.md#21-settings-and-toggle-reference) explains each control and the [upstream reference](UPSTREAMS.md) lists external services.

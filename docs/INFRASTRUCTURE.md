# Domains and organisations

[Documentation index](INDEX.md)

Domain cases can combine passive subdomain discovery, historical URL observations, authoritative RDAP registration metadata and bounded public site inspection. Specialist adapters are individually selectable; consult the collector catalogue before enabling them.

| Workflow | Evidence and limits |
| --- | --- |
| Passive domain discovery | Subfinder and theHarvester return bounded public-source results; not a vulnerability scan. |
| Historical URLs | Common Crawl, optional OTX/URLScan and Wayback observations; timestamps represent source observations. URLScan does not submit new scans. |
| Live Site Intelligence | Bounded same-domain public GET retrieval respecting robots.txt; retains source-attributed links, public identifiers and technology hints. No authentication attempts or active vulnerability scanning. |
| Certificate and email relationships | Public certificate transparency and mail-related DNS observations. Shared infrastructure is not proof of ownership. |
| RDAP and public IP/ASN | Registration and routing metadata; optional AbuseIPDB reputation. |
| Company information | Irish CRO, optional UK Companies House and GLEIF; registry records are not automatic identity matches. |
| VAT validation | Explicit EU/Northern Ireland VIES lookup of a supplied VAT number. |
| Authorised OpenPhish | Optional matching against an authorised configured feed, subject to its terms. |

Use the case-specific views and explicit enrichment actions. Check each observation against its original source before drawing conclusions. See [historical URLs](MANUAL.md#12-historical-url-intelligence), [site intelligence](MANUAL.md#13-live-site-intelligence) and [registration/infrastructure](MANUAL.md#14-registration-and-infrastructure-enrichment).

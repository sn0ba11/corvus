# Reporting and evidence exports

[Documentation index](INDEX.md)

Review the report preview only after examining source evidence, failed runs, limitations and analyst decisions. Corvus records source URLs, collector versions, timestamps, raw references and available hashes; these support review but do not by themselves establish authenticity or admissibility.

| Export | Purpose |
| --- | --- |
| HTML | Self-contained analytical report for local review or printing. |
| JSON | Structured case data for further processing. |
| CSV | Tabular findings for review. |
| Portable evidence bundle | Selected or complete findings with optional raw artifacts and SHA-256 manifest. |
| Dedicated TikTok ZIP | Individual acquired video and associated evidence files where supported. |
| Auto Archiver handoff | Local handoff bundle for an explicitly selected finding; does not automatically invoke Auto Archiver. |

For a portable bundle, selecting individual findings limits the selection; selecting none includes all. Review identifier, note and external URL redaction options before export. Raw artifacts can have additional size limits. Redaction does not mutate the source case and does not guarantee every sensitive detail in an attachment has been removed.

Keep exports and backups protected even after deleting the case. See [reports and exports](MANUAL.md#19-reports-and-exports), [bundle controls](MANUAL.md#evidence-bundle-controls) and [privacy](PRIVACY.md).

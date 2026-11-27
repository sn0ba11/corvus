# Contributing to Corvus

Contributions to collectors, evidence handling, analytical views, tests and documentation are welcome.

Start with the [developer guide](docs/DEVELOPERS.md) and [collector starter kit](examples/collector-template/README.md). For a new external source, explain its purpose, input types, output, access requirements, licence, network and privacy implications, and known limitations.

Use a focused change with synthetic fixtures and relevant regression tests. Specialist collectors should be opt-in unless there is a clear reason otherwise. Preserve existing evidence provenance and distinguish observed data from analyst interpretation.

Run the checks described in the developer guide before submitting a contribution. Do not include real case identifiers, session material, credentials or sensitive screenshots. Report vulnerabilities according to [SECURITY.md](SECURITY.md), not in a public issue.

Contributions must be compatible with the [project licence](LICENSE) and preserve third-party licence and attribution obligations. Do not bundle code or data without the required permissions.

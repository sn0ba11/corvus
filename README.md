# Corvus

<img src="app/static/corvus-mark.jpg" alt="Corvus" width="72">
Corvus is a local workbench for open-source investigations. Create a case, choose collectors, review their findings and provenance, and export evidence for further analysis. Local workflows include evidence imports, case graphs, timelines and report generation. Online collectors and the managed browser perform selected lookups against external services.

**[Download for Windows](https://github.com/sn0ba11/corvus/releases)** · [Installation guide](docs/DEPLOYMENT.md)

Choose the newest Windows setup executable from the release assets and verify its SHA-256 checksum before running it. Public pre-releases are available from the same page.

## Quick start

The core runs on Python 3.12 or later. The full stack uses Docker Engine with Compose v2 on Linux or Docker Desktop on Windows and macOS. Platform packages use the existing Windows and macOS launchers. See [installation](docs/DEPLOYMENT.md) for prerequisites, platform details and a Docker-free core run.

```sh
git clone https://github.com/sn0ba11/corvus.git
cd corvus
python3 scripts/installer.py install
```

Open <http://127.0.0.1:8000/> or the address shown by setup. Start with a synthetic case and disable online collectors when evaluating the UI offline.
![Corvus dashboard showing a completed synthetic investigation](docs/dashboard.png)

Linux runtime and silent Windows 11 installation have been validated. The macOS archive has been built and checked. Native macOS installation testing is deferred. Release packages and SHA-256 files are available on [GitHub Releases](https://github.com/sn0ba11/corvus/releases). For update commands, scheduling and rollback, see [updates](docs/UPDATER.md).

## External tools and data

The Compose stack builds collector images locally from pinned upstream source. Blackbird is separately enabled when the user has obtained a compatible installation. See the [integration reference](docs/UPSTREAMS.md) for tool, licence and network requirements. Site changes, sign-in walls and rate limits can make a collector inconclusive. Review source evidence before linking findings to a person or account.

Corvus stores its SQLite database, case exports and retained captures locally. A browser profile can hold authentication cookies. Selected collectors and browser actions send search terms or URLs to their respective upstream services. See [privacy and storage](docs/PRIVACY.md) for details.

Use Corvus only for lawful, authorised activity. Restrict access to the local workstation and secure its backups and exports. For configuration, health checks and troubleshooting, see [operations](docs/OPERATIONS.md) and the [operator manual](docs/MANUAL.md). Report security issues through the method listed in [SECURITY.md](SECURITY.md).

## Licence

Copyright (c) 2026 sn0ba11. Corvus uses the [PolyForm Noncommercial License 1.0.0](LICENSE), including the permitted purposes it defines for government, educational and public safety organisations. Commercial exploitation requires separate permission. Keep the [copyright notice](COPYRIGHT) with distributions.

Upstream tools retain their own licences. See [third-party notices](THIRD_PARTY_NOTICES.md) and [attribution](LICENSES/ATTRIBUTIONS.md).

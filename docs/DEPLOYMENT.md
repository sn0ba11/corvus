# Installation

Use Docker Engine with the Compose v2 plugin on Linux, or Docker Desktop with Linux containers on Windows or macOS. A source installation also needs Git and Python 3.12 or later. Docker builds download upstream packages, models and collector source from their public publishers.

## From source

```sh
git clone https://github.com/sn0ba11/corvus.git
cd corvus
python3 scripts/installer.py install
```

On Windows, run `powershell -NoProfile -File .\Install-OSINTWorkbench.ps1` from the checkout. Windows Docker Desktop uses WSL 2. For the packaged macOS path, extract the archive and run `Install-OSINTWorkbench.command`. See the release assets for checksums, platform requirements and package status. The macOS archive is unsigned.

The installers generate separate session and browser-agent secrets and create Docker volumes for case data and browser state. Open <http://127.0.0.1:8000/> unless setup reports another port. Setup runs under the local user. Installing prerequisites such as WSL or Docker Desktop can require administrator approval.

## Local core without Docker

From a clean checkout, use Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run --locked python scripts/start.py
```

This path provides the local application, database and built-in offline functions. Compose-backed tools need their own services. The Compose environment template uses container paths and should only be used for Compose installs.

See [privacy](PRIVACY.md), [update and rollback](UPDATER.md), and [integration setup](UPSTREAMS.md).

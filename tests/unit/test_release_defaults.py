from pathlib import Path

from app.core.config import Settings
from scripts import installer

ROOT = Path(__file__).resolve().parents[2]


def test_windows_updater_uses_package_version_not_stale_env_or_tags() -> None:
    source = (ROOT / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    block = source.split("function Get-SourceVersion {", 1)[1].split(
        "\nfunction Get-SourceCommit {", 1
    )[0]
    assert 'Join-Path $Root "pyproject.toml"' in block
    assert '$Matches[1] -eq "project"' in block
    assert 'return $Matches[1]' in block
    assert "Get-EnvValues" not in block
    assert "describe --tags" not in block


def test_fresh_install_generates_independent_local_secrets(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
    installer.ensure_env()
    fresh = installer.env_values()
    assert fresh["OSINT_VERSION"] == "0.2.0-rc14"
    settings = Settings(_env_file=tmp_path / ".env")
    assert settings.version == "0.2.0-rc14"

    assert fresh["OSINT_SECRET_KEY"] != fresh["OSINT_BROWSER_AGENT_TOKEN"]
    installer.ensure_env()
    updated = installer.env_values()
    assert updated["OSINT_SECRET_KEY"] == fresh["OSINT_SECRET_KEY"]
    assert updated["OSINT_BROWSER_AGENT_TOKEN"] == fresh["OSINT_BROWSER_AGENT_TOKEN"]

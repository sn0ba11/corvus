import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import build_updater, installer

ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> str:
    executable = shutil.which("git")
    assert executable
    return subprocess.run(  # noqa: S603
        [executable, "-c", "user.name=Updater Test", "-c", "user.email=updater@example.invalid", *args],
        cwd=root, check=True, text=True, capture_output=True,
    ).stdout.strip()


@pytest.fixture
def repositories(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "version.txt").write_text("one\n")
    (source / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    git(source, "add", ".")
    git(source, "commit", "-m", "Initial release")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(source), str(checkout))
    # Exercise URL validation while resolving only to a disposable local repository.
    git(checkout, "remote", "set-url", "origin", "https://corvus.example/source")
    git(checkout, "config", f"url.{source}.insteadOf", "https://corvus.example/source")
    monkeypatch.setattr(installer, "ROOT", checkout)
    original_run = installer.run

    def local_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if args == ["git", "remote", "get-url", "origin"]:
            return "https://corvus.example/source"
        return original_run(args, **kwargs)

    monkeypatch.setattr(installer, "run", local_run)
    return source, checkout


def advance(source: Path) -> str:
    (source / "version.txt").write_text("two\n")
    git(source, "commit", "-am", "Improve release")
    return git(source, "rev-parse", "HEAD")


def test_preview_fetches_without_merging_or_publishing(repositories, capsys):  # type: ignore[no-untyped-def]
    source, checkout = repositories
    before = git(checkout, "rev-parse", "HEAD")
    available = advance(source)
    assert installer.update_source(check_only=True)
    assert json.loads(capsys.readouterr().out) == {
        "current_commit": before, "available_commit": available,
        "commits_behind": 1, "update_available": True,
    }
    assert git(checkout, "rev-parse", "HEAD") == before
    assert not installer.pending_update_range_path().exists()
    assert not installer.pending_update_changelog_path().exists()


def test_update_fast_forwards_then_is_idempotent(repositories):  # type: ignore[no-untyped-def]
    source, checkout = repositories
    before = git(checkout, "rev-parse", "HEAD")
    available = advance(source)
    assert installer.update_source()
    assert git(checkout, "rev-parse", "HEAD") == available
    assert json.loads(installer.pending_update_range_path().read_text()) == {
        "from_commit": before, "to_commit": available, "fallback_used": False,
    }
    assert not installer.update_source()


@pytest.mark.parametrize("state", ["feature", "detached", "dirty", "ahead", "diverged"])
def test_unsafe_checkouts_are_preserved(repositories, state):  # type: ignore[no-untyped-def]
    source, checkout = repositories
    if state == "feature":
        git(checkout, "switch", "-c", "analyst-work")
    elif state == "detached":
        git(checkout, "checkout", "--detach")
    else:
        (checkout / "analyst-notes.txt").write_text("Preserve this evidence\n")
        if state in {"ahead", "diverged"}:
            git(checkout, "add", ".")
            git(checkout, "commit", "-m", "Local work")
    if state == "diverged":
        advance(source)
    before = git(checkout, "rev-parse", "HEAD")
    status = git(checkout, "status", "--porcelain")
    with pytest.raises(SystemExit):
        installer.update_source()
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "status", "--porcelain") == status


@pytest.mark.parametrize("failure", ["build", "up", "health"])
def test_failed_deployment_preserves_recovery_state(tmp_path, monkeypatch, failure):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(installer, "STATE", tmp_path / ".install-state.json")
    installer.STATE.write_text(json.dumps({"previous_images": {"app": "older"}}))
    previous = {"app": "sha256:currently-running"}
    for name in ("prerequisites", "ensure_update_changelog", "ensure_env", "backup"):
        monkeypatch.setattr(installer, name, lambda **kwargs: None)
    monkeypatch.setattr(installer, "snapshot_images", lambda: previous)
    monkeypatch.setattr(installer, "health", lambda: False)

    def compose(*args):  # type: ignore[no-untyped-def]
        assert json.loads(installer.STATE.read_text())["rollback_images"] == previous
        if args[0] == failure:
            raise subprocess.CalledProcessError(1, ["docker", "compose", *args])

    monkeypatch.setattr(installer, "compose", compose)
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        installer.update()
    state = json.loads(installer.STATE.read_text())
    assert state["rollback_images"] == previous
    assert "failed_update_at" in state
    assert not installer.STATE.with_suffix(".json.tmp").exists()


def test_rollback_prefers_images_from_failed_attempt(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "STATE", tmp_path / ".install-state.json")
    installer.STATE.write_text(json.dumps({
        "previous_images": {"app": "older"}, "rollback_images": {"app": "last-working"},
    }))
    for name in ("prerequisites", "ensure_update_changelog", "ensure_env", "compose"):
        monkeypatch.setattr(installer, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "health", lambda: True)
    calls = []
    monkeypatch.setattr(installer, "run", lambda args: calls.append(args))
    installer.rollback()
    assert calls == [["docker", "image", "tag", "last-working", installer.IMAGE_TAGS["app"]]]


@pytest.mark.parametrize("pending,stale,deploys", [(False, False, False), (True, False, True), (False, True, True)])
def test_scheduled_retry_checks_range_and_running_build(tmp_path, monkeypatch, pending, stale, deploys):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["installer.py", "update", "--only-if-changed"])
    monkeypatch.setattr(installer, "update_source", lambda _: False)
    monkeypatch.setattr(installer, "source_commit", lambda: "current")
    monkeypatch.setattr(installer, "env_values", lambda: {"OSINT_BUILD_COMMIT": "old" if stale else "current"})
    if pending:
        installer.pending_update_range_path().write_text("{}")
    calls = []
    monkeypatch.setattr(installer, "update", lambda **kwargs: calls.append(True))
    installer.main()
    assert bool(calls) == deploys


def test_updater_lock_releases_after_failure(tmp_path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("corvus_updater", ROOT / "Update-Corvus.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError), module.update_lock(tmp_path):
        with pytest.raises(SystemExit, match="already running"):
            with module.update_lock(tmp_path):
                pytest.fail("Concurrent lock acquired")
        raise RuntimeError("Deployment failed")
    with module.update_lock(tmp_path):
        pass


def test_package_is_reproducible_with_verified_payloads(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    commit = "a" * 40

    def fake_git(*args):  # type: ignore[no-untyped-def]
        if args[0] == "rev-parse":
            return commit.encode()
        if args[0] == "ls-tree":
            return b"LICENSES/ATTRIBUTIONS.md\nLICENSES/HTMX.txt\n"
        return (ROOT / args[1].split(":", 1)[1]).read_bytes()

    monkeypatch.setattr(build_updater, "git", fake_git)
    first = build_updater.build(tmp_path / "first")
    second = build_updater.build(tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["commit"] == commit
        assert "LICENSES/HTMX.txt" in manifest["files"]
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == digest
    assert first.with_suffix(".zip.sha256").read_text().startswith(hashlib.sha256(first.read_bytes()).hexdigest())


@pytest.mark.parametrize("state", ["normal", "feature", "ahead"])
def test_native_powershell_source_update(repositories, state):  # type: ignore[no-untyped-def]
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    source, checkout = repositories
    available = advance(source)
    if state == "feature":
        git(checkout, "switch", "-c", "analyst-work")
    elif state == "ahead":
        (checkout / "notes.txt").write_text("preserve\n")
        git(checkout, "add", ".")
        git(checkout, "commit", "-m", "Local analyst work")
    before = git(checkout, "rev-parse", "HEAD")
    environment = os.environ.copy()
    environment.update({"CORVUS_TEST_ROOT": str(checkout), "CORVUS_TEST_SCRIPT": str(ROOT / "Update-OSINTWorkbench.ps1")})
    # Load only the actual source-update functions. Docker/deployment code is not evaluated.
    script = r'''
$ErrorActionPreference = 'Stop'
$root = $env:CORVUS_TEST_ROOT
$DefaultRepositoryUrl = 'https://github.com/sn0ba11/corvus.git'
$UpdateBranch = 'main'
$pendingChangeLogPath = Join-Path $root '.pending-update-changes.txt'
$pendingRangePath = Join-Path $root '.pending-update-range.json'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CORVUS_TEST_SCRIPT, [ref]$null, [ref]$null)
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($function in $functions) {
    if ($function.Name -in @('Set-Origin', 'Update-Source')) {
        . ([scriptblock]::Create($function.Extent.Text))
    }
}
function Ensure-GitExecutable { (Get-Command git).Source }
function Invoke-Git([string[]]$Arguments, [switch]$Capture, [switch]$IgnoreExitCode) {
    if (($Arguments -join ' ') -eq 'remote get-url origin') { return 'https://corvus.example/source' }
    $output = & git -C $root @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($IgnoreExitCode) { return '' }
        throw "Git failed: $output"
    }
    if ($Capture) { return ($output -join [Environment]::NewLine).Trim() }
}
try { $changed = Update-Source; Write-Output "changed=$changed" } catch { Write-Output $_; exit 1 }
'''
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-Command", script],  # noqa: S603
                            env=environment, capture_output=True, text=True)
    if state == "normal":
        assert result.returncode == 0, result.stdout + result.stderr
        assert git(checkout, "rev-parse", "HEAD") == available
        assert "changed=True" in result.stdout
    else:
        assert result.returncode != 0
        assert git(checkout, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("exit_code", [0, 7])
def test_windows_package_launcher_propagates_result(tmp_path, exit_code):  # type: ignore[no-untyped-def]
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    (tmp_path / ".git").mkdir()
    (tmp_path / "Update-Corvus.ps1").write_text(f"exit {exit_code}\n")
    result = subprocess.run(  # noqa: S603
        [powershell, "-NoLogo", "-NoProfile", "-File",
         str(ROOT / "installer/windows/Update-CorvusLauncher.ps1"), "-InstallRoot", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) == (exit_code == 0), result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["build", "up", "health", "provenance"])
def test_native_powershell_records_recovery_before_deployment(tmp_path, failure):  # type: ignore[no-untyped-def]
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    state_path = tmp_path / ".install-state.json"
    state_path.write_text(json.dumps({"build_commit": "last-installed", "previous_images": {"app": "older"}}))
    environment = os.environ.copy()
    environment.update({"CORVUS_TEST_STATE": str(state_path), "CORVUS_TEST_FAILURE": failure,
                        "CORVUS_TEST_SCRIPT": str(ROOT / "OSINTWorkbench.ps1")})
    script = r'''
$ErrorActionPreference = 'Stop'
$StatePath = $env:CORVUS_TEST_STATE
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CORVUS_TEST_SCRIPT, [ref]$null, [ref]$null)
$functions = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($function in $functions) {
    if ($function.Name -in @('Save-State', 'Update-Workbench')) { . ([scriptblock]::Create($function.Extent.Text)) }
}
foreach ($name in @('Assert-Prerequisites', 'Ensure-UpdateChangelog', 'Ensure-Env', 'Assert-ConfiguredPorts',
                    'Sync-BrowserDesktopUrl', 'Backup-Database', 'Write-ComposeDiagnostics')) {
    . ([scriptblock]::Create("function $name { }"))
}
function Get-ImageSnapshot { return @{ app = 'sha256:last-working' } }
function Get-ChangedBuildServices { return @('app') }
function Get-SourceCommit { return ('a' * 40) }
function Get-RunningBuildCommit { return ('a' * 40) }
function Invoke-ComposeWithRetry([string[]]$Arguments) {
    $record = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    if ($record.rollback_images.app -ne 'sha256:last-working') { throw 'Recovery record missing before Docker' }
    if ($Arguments[0] -eq $env:CORVUS_TEST_FAILURE) { throw 'Simulated Docker failure' }
}
function Wait-Healthy { return ($env:CORVUS_TEST_FAILURE -ne 'health') }
function Assert-RunningBuildMatchesSource { if ($env:CORVUS_TEST_FAILURE -eq 'provenance') { throw 'Wrong running build' } }
try { Update-Workbench; exit 0 } catch { Write-Output $_; exit 9 }
'''
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-Command", script],  # noqa: S603
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 9, result.stdout + result.stderr
    state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    assert state.get("rollback_images") == {"app": "sha256:last-working"}, result.stdout + result.stderr
    assert state["build_commit"] == "last-installed"
    assert "failed_update_at" in state
    assert not state_path.with_suffix(".json.tmp").exists()


def _load_updater():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("corvus_updater_cli", ROOT / "Update-Corvus.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli(monkeypatch, tmp_path, argv, stdout=""):  # type: ignore[no-untyped-def]
    module = _load_updater()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "installer.py").write_bytes((ROOT / "scripts" / "installer.py").read_bytes())
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.os, "name", "posix")
    monkeypatch.setattr(sys, "argv", ["Update-Corvus.py", "--install-dir", str(tmp_path), *argv])
    return module, calls


@pytest.mark.parametrize(("flags", "verb"), [([], "update"), (["--check"], "check-update")])
def test_cli_forwards_repository_url_to_installer(monkeypatch, tmp_path, flags, verb):  # type: ignore[no-untyped-def]
    module, calls = _run_cli(monkeypatch, tmp_path, [*flags, "--repository-url", "https://git.example/c.git"])
    assert module.main() == 0
    assert calls[0][1:] == [str(tmp_path / "scripts" / "installer.py"), verb,
                            "--repository-url", "https://git.example/c.git"]


def test_cli_rejects_invalid_repository_url_and_runs_nothing(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    module, calls = _run_cli(monkeypatch, tmp_path, ["--repository-url", "--upload-pack=evil"])
    with pytest.raises(SystemExit) as raised:
        module.main()
    assert raised.value.code == 2
    assert calls == []


def test_cli_show_source_prints_origin_without_fetching(monkeypatch, tmp_path, capsys):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/git")
    module, calls = _run_cli(monkeypatch, tmp_path, ["--show-source"], stdout="https://git.example/c.git\n")
    assert module.main() == 0
    assert capsys.readouterr().out.strip() == "https://git.example/c.git"
    assert calls == [["git", "remote", "get-url", "origin"]]

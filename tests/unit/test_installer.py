import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import installer


def test_health_accepts_all_services_as_compose_ndjson(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    payload = "\n".join(
        json.dumps({"Service": service, "State": "running", "Health": "healthy"})
        for service in installer.CORE_SERVICES
    )
    monkeypatch.setattr(installer, "compose", lambda *args, **kwargs: payload)
    assert installer.health(deadline=1)


def test_health_requires_integral_browser(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    without_browser = "\n".join(
        json.dumps({"Service": service, "State": "running", "Health": "healthy"})
        for service in installer.CORE_SERVICES
        if service != "browser-agent"
    )
    monkeypatch.setattr(installer, "compose", lambda *args, **kwargs: without_browser)
    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(installer.time, "sleep", lambda _: None)
    assert not installer.health(deadline=1)


def test_repository_url_validation() -> None:
    assert installer.DEFAULT_REPOSITORY_URL == "https://github.com/sn0ba11/corvus.git"
    assert installer.repository_url_valid("https://git.example/author/osint-workbench.git")
    assert installer.repository_url_valid("git@git.example:author/osint-workbench.git")
    assert not installer.repository_url_valid("--upload-pack=malicious")
    assert not installer.repository_url_valid("file:///tmp/untrusted")


def test_cross_platform_updater_preserves_existing_origin() -> None:
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert 'tracking_ref = f"refs/remotes/origin/{UPDATE_BRANCH}"' in source
    assert '["git", "fetch", "--prune", "origin", f"{branch_ref}:{tracking_ref}"]' in source
    assert '["git", "merge", "--ff-only", tracking_ref]' in source
    assert "@@CHANGE@@%n%ad%n%H%n%s%n%b%n@@END@@" in source


def test_cross_platform_updater_refreshes_origin_main_before_merge(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/git")
    calls: list[list[str]] = []
    previous_commit = installer.UNKNOWN_BUILD_COMMIT
    current_commit = "facefeed01234567890123456789012345678901"
    head_values = iter((previous_commit, current_commit))

    def fake_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
        calls.append(args)
        if args == ["git", "branch", "--show-current"]:
            return "main"
        if args[:2] == ["git", "rev-list"]:
            return "0 1"
        if args == ["git", "remote", "get-url", "origin"]:
            return "https://git.example/author/osint-workbench.git"
        if args == ["git", "status", "--porcelain", "--untracked-files=normal"]:
            return ""
        if args == ["git", "rev-parse", "HEAD"]:
            return next(head_values)
        if args == ["git", "rev-parse", "refs/remotes/origin/main"]:
            return current_commit
        if args[:2] == ["git", "log"]:
            return "2026-08-06  abc1234  Update updater tracking"
        return ""

    monkeypatch.setattr(installer, "run", fake_run)

    assert installer.update_source()
    assert [
        "git",
        "fetch",
        "--prune",
        "origin",
        "refs/heads/main:refs/remotes/origin/main",
    ] in calls
    assert ["git", "merge", "--ff-only", "refs/remotes/origin/main"] in calls
    assert ["git", "config", "branch.main.remote", "origin"] in calls
    assert ["git", "config", "branch.main.merge", "refs/heads/main"] in calls
    staged_range = json.loads(installer.pending_update_range_path().read_text(encoding="utf-8"))
    assert staged_range["from_commit"] == previous_commit
    assert staged_range["to_commit"] == current_commit
    assert staged_range["fallback_used"] is False


def test_initial_update_changelog_is_created_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)

    installer.ensure_update_changelog()
    path = tmp_path / ".last-update-changes.txt"
    runtime_path = tmp_path / "runtime" / "last-update-changes.txt"
    range_path = tmp_path / ".last-update-range.json"
    runtime_range_path = tmp_path / "runtime" / "last-update-range.json"
    assert "Initial installation" in path.read_text(encoding="utf-8")
    assert runtime_path.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
    build_range = json.loads(range_path.read_text(encoding="utf-8"))
    assert build_range == {
        "from_commit": installer.UNKNOWN_BUILD_COMMIT,
        "to_commit": installer.UNKNOWN_BUILD_COMMIT,
        "fallback_used": False,
    }
    assert runtime_range_path.read_text(encoding="utf-8") == range_path.read_text(encoding="utf-8")

    path.write_text("retained update\n", encoding="utf-8")
    installer.ensure_update_changelog()
    assert path.read_text(encoding="utf-8") == "retained update\n"
    assert runtime_path.read_text(encoding="utf-8") == "retained update\n"


def test_existing_untracked_install_uses_one_time_baseline_fallback(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".install-state.json").write_text("{}\n", encoding="utf-8")

    installer.ensure_update_changelog()

    build_range = json.loads(installer.update_range_path().read_text(encoding="utf-8"))
    assert build_range == {
        "from_commit": installer.UNKNOWN_BUILD_COMMIT,
        "to_commit": installer.UNKNOWN_BUILD_COMMIT,
        "fallback_used": True,
    }


def test_update_changelog_is_printed_and_retained(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(
        installer,
        "run",
        lambda *args, **kwargs: "2026-08-05  abc1234  Improve collector parsing",
    )

    previous_commit = installer.UNKNOWN_BUILD_COMMIT
    current_commit = "facefeed01234567890123456789012345678901"
    installer._print_update_changelog(previous_commit, current_commit)

    rendered = (tmp_path / ".last-update-changes.txt").read_text(encoding="utf-8")
    assert "Corvus changes installed" in rendered
    assert "Improve collector parsing" in rendered
    assert (tmp_path / "runtime" / "last-update-changes.txt").read_text(
        encoding="utf-8"
    ) == rendered
    assert "Improve collector parsing" in capsys.readouterr().out
    build_range = json.loads(installer.update_range_path().read_text(encoding="utf-8"))
    assert build_range["from_commit"] == previous_commit
    assert build_range["to_commit"] == current_commit


def test_staged_update_changelog_is_published_only_after_success(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setattr(
        installer,
        "run",
        lambda *args, **kwargs: "2026-08-06  abc1234  Add dashboard changelog",
    )
    installer.ensure_update_changelog()
    previous = installer.update_changelog_path().read_text(encoding="utf-8")

    previous_commit = installer.UNKNOWN_BUILD_COMMIT
    current_commit = "facefeed01234567890123456789012345678901"
    installer._stage_update_changelog(previous_commit, current_commit)

    assert installer.update_changelog_path().read_text(encoding="utf-8") == previous
    assert installer.pending_update_changelog_path().is_file()
    assert installer.pending_update_range_path().is_file()
    installer.promote_pending_update_changelog()
    assert not installer.pending_update_changelog_path().exists()
    assert not installer.pending_update_range_path().exists()
    assert "Add dashboard changelog" in installer.update_changelog_path().read_text(
        encoding="utf-8"
    )
    assert "Add dashboard changelog" in installer.runtime_update_changelog_path().read_text(
        encoding="utf-8"
    )
    assert "Add dashboard changelog" in capsys.readouterr().out
    build_range = json.loads(installer.update_range_path().read_text(encoding="utf-8"))
    assert build_range["from_commit"] == previous_commit
    assert build_range["to_commit"] == current_commit
    assert installer.runtime_update_range_path().read_text(
        encoding="utf-8"
    ) == installer.update_range_path().read_text(encoding="utf-8")


def test_configured_url_reads_valid_port(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("OSINT_PORT=8123\n", encoding="utf-8")
    assert installer.configured_url() == "http://127.0.0.1:8123"


def test_available_port_falls_back_to_os_assigned_loopback_port(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "port_available", lambda _port: False)
    selected = installer.available_port(8000, 8001)
    assert 1 <= selected <= 65535
    assert selected not in {8000, 8001}


def test_fresh_cross_platform_install_selects_free_loopback_ports(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "OSINT_PORT=8000\nOSINT_BROWSER_DESKTOP_PORT=6080\n",
        encoding="utf-8",
    )
    unavailable = {8000, 6080}
    monkeypatch.setattr(installer, "port_available", lambda port: port not in unavailable)

    installer.ensure_available_ports()

    values = installer.env_values()
    assert values["OSINT_PORT"] == "8001"
    assert values["OSINT_BROWSER_DESKTOP_PORT"] == "6081"
    assert values["OSINT_BROWSER_DESKTOP_URL"] == (
        "http://127.0.0.1:6081/vnc.html?autoconnect=true&resize=scale"
    )


def test_default_install_enables_browser_and_generates_local_secrets(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "OSINT_SECRET_KEY=replace-with-at-least-32-random-characters\n"
        "OSINT_BROWSER_ENABLED=false\n"
        "OSINT_LOG_QUERIES=true\n"
        "OSINT_BROWSER_AGENT_TOKEN=replace-with-browser-agent-token\n",
        encoding="utf-8",
    )
    installer.ensure_env()
    values = installer.env_values()
    assert values["OSINT_BROWSER_ENABLED"] == "true"
    assert values["OSINT_LOG_QUERIES"] == "true"
    assert values["OSINT_BUILD_COMMIT"] == installer.UNKNOWN_BUILD_COMMIT
    assert len(values["OSINT_SECRET_KEY"]) >= 32
    assert len(values["OSINT_BROWSER_AGENT_TOKEN"]) >= 32
    assert "OSINT_BROWSER_VNC_PASSWORD" not in values
    assert "replace-with" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_existing_env_merges_current_browser_allowlist_defaults(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "OSINT_SECRET_KEY=replace-with-at-least-32-random-characters\n"
        "OSINT_BROWSER_AGENT_TOKEN=replace-with-browser-agent-token\n"
        "OSINT_BROWSER_ALLOWED_HOSTS=google.com,duckduckgo.com,brave.com,qwant.com,startpage.com\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "OSINT_SECRET_KEY=" + "x" * 48 + "\n"
        "OSINT_BROWSER_AGENT_TOKEN=" + "y" * 48 + "\n"
        "OSINT_BROWSER_ALLOWED_HOSTS=google.com\n",
        encoding="utf-8",
    )
    installer.ensure_env()
    hosts = installer.env_values()["OSINT_BROWSER_ALLOWED_HOSTS"].split(",")
    assert hosts == ["google.com", "duckduckgo.com", "brave.com", "qwant.com", "startpage.com"]


def test_windows_bootstrap_elevates_wsl_package_install_and_gates_setup_on_docker() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "installer" / "windows" / "Bootstrap-OSINTWorkbench.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Start-Process -FilePath $wingetExecutable -ArgumentList $wingetArgs -Verb RunAs -Wait -PassThru' in script
    assert "Show-DockerRetryDialog" in script
    assert "MessageBoxButtons]::RetryCancel" in script
    assert "Click Cancel to stop setup." in script

    docker_gate = script.index("Start-AndWaitDockerDesktop -TimeoutSeconds 240")
    assert docker_gate < script.index('The embedded application checkout is ready')
    assert "Setup was cancelled." in script


def test_windows_installer_generates_required_searxng_secret() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    assert "OSINT_SEARXNG_SECRET" in script
    assert "replace-with-a-distinct-searxng-secret" in script
    assert 'Set-EnvValue $content "OSINT_SEARXNG_SECRET" (New-RandomToken 48)' in script


def test_windows_installers_avoid_powershell_51_split_overload() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in (
        "OSINTWorkbench.ps1",
        "Install-OSINTWorkbench.ps1",
        "Register-OSINTDailyUpdate.ps1",
        "Update-Corvus.ps1",
        "Update-OSINTWorkbench.ps1",
    ):
        script = (root / name).read_text(encoding="utf-8")
        assert '.Split(@("="), 2' not in script


def test_macos_starts_docker_desktop_with_native_open(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> SimpleNamespace:  # type: ignore[no-untyped-def]
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer.start_docker_desktop()
    assert calls == [["/usr/bin/open", "-gj", "-a", "Docker"]]


def test_macos_waits_for_docker_engine_after_launch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    states = iter((False, True))
    clock = [0.0]

    def monotonic() -> float:
        clock[0] += 1.0
        return clock[0]

    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer, "docker_engine_available", lambda: next(states))
    monkeypatch.setattr(installer, "start_docker_desktop", lambda: True)
    monkeypatch.setattr(installer.time, "monotonic", monotonic)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    installer.ensure_docker_engine(deadline=10)


def test_compose_uses_standard_service_set_without_profile(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
        calls.append(args)
        return ""

    monkeypatch.setattr(installer, "run", fake_run)
    installer.compose("ps")
    assert calls == [["docker", "compose", "ps"]]


def test_backup_uses_recovery_container_when_app_is_stopped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(installer, "service_running", lambda service: False)
    monkeypatch.setattr(installer.time, "strftime", lambda *args: "20260727T122400Z")
    monkeypatch.setattr(installer, "compose", lambda *args, **kwargs: calls.append(args))

    installer.backup()

    assert len(calls) == 1
    assert calls[0][:7] == (
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "app",
        "-c",
    )


def test_powershell_backup_recovers_when_app_is_stopped() -> None:
    root = Path(__file__).resolve().parents[2]
    powershell = (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    assert "function Test-ServiceRunning([string]$Service)" in powershell
    assert 'if (Test-ServiceRunning "app")' in powershell
    assert (
        '@("run", "--rm", "--no-deps", "--entrypoint", "python", "app", "-c", $code)' in powershell
    )


def test_compose_and_powershell_make_browser_integral() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    powershell = (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    assert 'profiles: ["browser"]' not in compose
    assert 'OSINT_BROWSER_ENABLED: "true"' in compose
    assert "OSINT_BROWSER_VNC_PASSWORD" not in compose
    assert "OSINT_BUILD_COMMIT: ${OSINT_BUILD_COMMIT:-}" in compose
    assert "browser-agent: {condition: service_healthy}" in compose
    assert '"instaloader", "gallery-dl", "yt-dlp", "nexfil", "social_analyzer", "searxng", "browser-agent"' in powershell
    assert "gallery-dl: {condition: service_healthy}" in compose
    assert "yt-dlp: {condition: service_healthy}" in compose
    assert "image: corvus-gallery-dl:latest" in compose
    assert "image: corvus-yt-dlp:latest" in compose
    assert '"subfinder" = "corvus-subfinder:latest"' in powershell
    assert '"searxng" = "corvus-searxng:latest"' in powershell
    assert "subfinder: {condition: service_healthy}" in compose
    assert "searxng: {condition: service_healthy}" in compose
    assert "image: corvus-searxng:latest" in compose
    assert "./runtime:/runtime:ro" in compose
    assert "function Ensure-UpdateChangelog" in powershell
    assert 'Join-Path $Root "runtime"' in powershell
    assert '$buildCommit = if ($BuildCommitOverride' in powershell
    assert 'Set-EnvValue $content "OSINT_BUILD_COMMIT" $buildCommit' in powershell
    assert '".last-update-range.json"' in powershell
    assert (
        'ports: ["127.0.0.1:'
        not in compose.split("  searxng:", 1)[1].split("  browser-agent:", 1)[0]
    )
    assert '--profile", "browser"' not in powershell


def test_operator_manual_ships_in_container() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    manual = (root / "docs" / "MANUAL.md").read_text(encoding="utf-8")

    assert "COPY CHANGELOG.md ./CHANGELOG.md" in dockerfile
    assert "COPY docs/MANUAL.md ./docs/MANUAL.md" in dockerfile
    assert "# Corvus Operator Manual" in manual
    assert "CAPTCHA and manual sock-browser workflow" in manual
    assert "Created by Nova Example" not in manual


def test_tool_runner_honours_configurable_one_hour_timeout_ceiling() -> None:
    root = Path(__file__).resolve().parents[2]
    runner = (root / "tool_containers" / "runner.py").read_text(encoding="utf-8")
    assert "MAX_JOB_TIMEOUT_SECONDS = 3600" in runner
    assert "min(MAX_JOB_TIMEOUT_SECONDS" in runner


def test_windows_checkout_preserves_all_container_entrypoints_as_lf() -> None:
    root = Path(__file__).resolve().parents[2]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")
    browser_dockerfile = (root / "browser_agent" / "Dockerfile").read_text(encoding="utf-8")
    tool_dockerfile = (root / "tool_containers" / "Dockerfile").read_text(encoding="utf-8")
    assert "*.py text eol=lf" in attributes
    assert "*.sh text eol=lf" in attributes
    assert "sed -i 's/\\r$//'" in browser_dockerfile
    assert "sed -i 's/\\r$//'" in tool_dockerfile
    assert "/usr/local/bin/instaloader-osint" in tool_dockerfile
    assert "/usr/local/bin/social-analyzer-osint" in tool_dockerfile
    assert (root / "tool_containers" / "Dockerfile.subfinder").exists()


def test_windows_updater_is_promptless_and_fast_forward_only() -> None:
    root = Path(__file__).resolve().parents[2]
    updater = (root / "Update-OSINTWorkbench.ps1").read_text(encoding="utf-8")
    scheduler = (root / "Register-OSINTDailyUpdate.ps1").read_text(encoding="utf-8")
    assert '$DefaultRepositoryUrl = "https://github.com/sn0ba11/corvus.git"' in updater
    assert "Read-Host" not in updater
    assert "elseif ($origin)" in updater
    assert "$DefaultRepositoryUrl" in updater
    assert 'Write-Host "Installed commit:' in updater
    assert 'Write-Host "Available commit:' in updater
    assert "Source update verification failed" in updater
    assert '$trackingRef = "refs/remotes/origin/$UpdateBranch"' in updater
    assert '@("fetch", "--prune", "origin", $refspec)' in updater
    assert '@("merge", "--ff-only", $trackingRef)' in updater
    assert '"branch.$UpdateBranch.remote", "origin"' in updater
    assert '@("status", "--porcelain", "--untracked-files=no")' in updater
    assert '$gitArguments = @("-c", "safe.directory=$root") + $Arguments' in updater
    assert "tracked local changes" in updater
    assert "$OnlyIfChanged -and -not $sourceChanged -and -not $appMetadataStale" in updater
    assert '$env:GIT_TERMINAL_PROMPT = "0"' in updater
    assert '$env:GCM_INTERACTIVE = "never"' in updater
    assert "function Find-GitExecutable" in updater
    assert "function Ensure-GitExecutable" in updater
    assert 'Join-Path $env:LOCALAPPDATA "Programs\\Git\\cmd\\git.exe"' in updater
    assert '"install", "--id", "Git.Git"' in updater
    assert '& $script:GitExecutable @gitArguments' in updater
    assert '$ErrorActionPreference = "Continue"' in updater
    assert '"log", "--no-merges", "--date=short"' in updater
    assert "@@CHANGE@@%n%ad%n%H%n%s%n%b%n@@END@@" in updater
    assert '".last-update-changes.txt"' in updater
    assert '".pending-update-changes.txt"' in updater
    assert '".pending-update-range.json"' in updater
    assert '".last-update-range.json"' in updater
    assert '$appMetadataStale = $currentHead' in updater
    assert 'Running build metadata is stale' in updater
    assert '-ForceAppRebuild:($sourceChanged -or $appMetadataStale)' in updater
    assert '-BuildCommitOverride $currentHead' in updater
    assert 'Join-Path $root "runtime"' in updater
    assert "Corvus changes installed" in updater
    assert "Move-Item -LiteralPath $pendingChangeLogPath" in updater
    assert "Move-Item -LiteralPath $pendingRangePath" in updater
    assert "from_commit = $previous" in updater
    assert "to_commit = $current" in updater
    assert '$trimmed.StartsWith("[")' in (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    assert "$line | ConvertFrom-Json -ErrorAction Stop" in (root / "OSINTWorkbench.ps1").read_text(
        encoding="utf-8"
    )
    workbench = (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    assert '"up", "-d", "--force-recreate", "--remove-orphans"' not in workbench
    assert 'Invoke-ComposeWithRetry -Arguments @("up", "-d", "--remove-orphans")' in workbench
    assert "function Get-ChangedBuildServices" in workbench
    assert '[switch]$ForceAppRebuild' in workbench
    assert "function Get-RunningBuildCommit" in workbench
    assert '$staleRunningApp = [bool]($runningCommit -and $sourceCommit -and $runningCommit -ne $sourceCommit)' in workbench
    assert 'if (($ForceAppRebuild -or $staleRunningApp) -and $buildServices -notcontains "app")' in workbench
    assert 'Write-Host "Running app is stale' in workbench
    assert 'git -c "safe.directory=$Root" diff --name-only "$fromCommit..$toCommit"' in workbench
    assert 'Write-Host "Rebuilding changed Corvus images:' in workbench
    assert 'Write-Host "No container image inputs changed; skipping image rebuild."' in workbench
    assert 'Set-EnvValue $content "OSINT_VERSION" (Get-SourceVersion)' in workbench
    assert '$buildCommit = if ($BuildCommitOverride' in workbench
    assert 'Set-EnvValue $content "OSINT_BUILD_COMMIT" $buildCommit' in workbench
    source_commit_block = workbench.split("function Get-SourceCommit", 1)[1].split("function Get-Services", 1)[0]
    assert 'Get-Command git -ErrorAction SilentlyContinue' in source_commit_block
    assert 'Get-Command git.exe -CommandType Application' not in source_commit_block
    assert '$path -eq "docs/MANUAL.md"' in workbench
    assert '$null = $selected.Add("app")' in workbench
    assert "Assert-RunningBuildMatchesSource" in workbench
    assert "Running Corvus build verified" in workbench
    assert 'compose("up", "-d", "--force-recreate", "--remove-orphans")' in (
        root / "scripts" / "installer.py"
    ).read_text(encoding="utf-8")
    assert "-WindowStyle Hidden" in scheduler
    assert "-NonInteractive -OnlyIfChanged -ScheduledRun" in scheduler
    assert '-RepositoryUrl `"$RepositoryUrl`"' in scheduler
    assert "[string]$RepositoryUrl = " not in scheduler


def test_windows_installer_handles_incomplete_release_and_optional_setup_failures() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "Install-OSINTWorkbench.ps1").read_text(encoding="utf-8")

    assert "$scriptPath = $PSCommandPath" in script
    assert "do not paste it into a PowerShell prompt" in script
    assert "Extract or clone the complete release before running the installer" in script
    assert "$dailyUpdateAvailable = $false" in script
    assert "Daily-update support could not be loaded" in script
    assert "Installation will continue without the daily update task" in script
    assert "desktop shortcuts could not be created" in script
    assert "daily update task could not be registered" in script
    assert '. "$root\\Register-OSINTDailyUpdate.ps1"' not in script


def test_standard_windows_installer_embeds_updateable_checkout_and_supports_full_reset() -> None:
    root = Path(__file__).resolve().parents[2]
    bootstrap = (root / "installer" / "windows" / "Bootstrap-OSINTWorkbench.ps1").read_text(
        encoding="utf-8"
    )
    workbench = (root / "OSINTWorkbench.ps1").read_text(encoding="utf-8")
    uninstaller = (root / "installer" / "windows" / "Uninstall-OSINTWorkbench.ps1").read_text(
        encoding="utf-8"
    )
    nsis = (root / "installer" / "windows" / "OSINTWorkbench.nsi").read_text(encoding="utf-8")
    builder = (root / "Build-WindowsInstaller.ps1").read_text(encoding="utf-8")
    icon_generator = (root / "installer" / "windows" / "New-OSINTWorkbenchIcon.ps1").read_text(
        encoding="utf-8"
    )
    portable_icon_generator = (root / "scripts" / "new_osint_workbench_icon.py").read_text(
        encoding="utf-8"
    )
    protocol_registration = (root / "Register-OSINTUpdateProtocol.ps1").read_text(encoding="utf-8")
    updater = (root / "Update-OSINTWorkbench.ps1").read_text(encoding="utf-8")
    update_bootstrap = (root / "installer" / "windows" / "Update-CorvusBootstrap.ps1").read_text(encoding="utf-8")

    assert 'InstallDir "$LOCALAPPDATA\\Programs\\Corvus"' in nsis
    assert "RequestExecutionLevel user" in nsis
    assert "osint-workbench.bundle" in nsis
    assert "InitPluginsDir" in nsis
    assert 'IfFileExists "$PLUGINSDIR\\osint-workbench.bundle"' in nsis
    assert "Bootstrap-OSINTWorkbench.ps1" in nsis
    assert "Update-CorvusBootstrap.ps1" in nsis
    assert "Corvus Update.lnk" in nsis
    assert "Software\\Classes\\corvus-update" in nsis
    assert "URL:Corvus Update" in nsis
    assert "Uninstall Corvus.exe" in nsis
    assert 'Join-Path $InstallRoot "app"' in bootstrap
    assert '"clone", "--branch", "main", "--single-branch", $BundlePath, $appRoot' in bootstrap
    assert '"merge", "--ff-only", "refs/remotes/installer/main"' in bootstrap
    assert '"stash", "push", "--include-untracked"' in bootstrap
    assert '"stash", "apply", $savedStash' in bootstrap
    assert "Corvus installer backup" in bootstrap
    assert "Local configuration and Docker volumes are unchanged." in bootstrap
    assert "The existing Corvus checkout has local changes." not in bootstrap
    assert '"remote", "set-url", "origin", $RepositoryUrl' in bootstrap
    assert "Find-AvailablePort 8000 8099" in bootstrap
    assert "function Ensure-AvailablePorts" in workbench
    assert "function Assert-ConfiguredPorts" in workbench
    assert "function Sync-BrowserDesktopUrl" in workbench
    assert "function Test-LoopbackPortAvailable" in workbench
    install_block = workbench.split("function Install-Workbench", 1)[1].split("function Update-Workbench", 1)[0]
    update_block = workbench.split("function Update-Workbench", 1)[1].split("function Rollback-Workbench", 1)[0]
    assert "Ensure-AvailablePorts" in install_block
    assert "Ensure-AvailablePorts" not in update_block
    assert "Assert-ConfiguredPorts" in update_block
    assert "Sync-BrowserDesktopUrl" in update_block
    assert 'OSINT_BROWSER_DESKTOP_URL" "http://127.0.0.1:$browserPort/vnc.html?autoconnect=true&resize=scale"' in workbench
    assert '"-NoShortcuts", "-NoLaunch"' in bootstrap
    assert "function Start-AndWaitDockerDesktop" in bootstrap
    assert "function Test-DockerEngineReady([int]$ProbeTimeoutSeconds = 6)" in bootstrap
    assert ".WaitForExit($ProbeTimeoutSeconds * 1000)" in bootstrap
    assert "Diagnostics.ProcessStartInfo" in bootstrap
    assert "stopping the unresponsive client" in bootstrap
    assert "taskkill.exe /PID $process.Id /T /F" in bootstrap
    assert "function Show-DockerRetryDialog" in bootstrap
    assert "MessageBoxButtons]::RetryCancel" in bootstrap
    assert "Retry selected. Checking Docker Desktop's Linux-container engine again" in bootstrap
    assert "Setup was cancelled" in bootstrap
    assert "function Start-AndWaitDockerDesktop([int]$TimeoutSeconds = 60)" in bootstrap
    assert "Phase 1/5: installing and validating Git, WSL 2 and Docker Desktop prerequisites" in bootstrap
    assert "function Install-AndEnsureWsl2" in bootstrap
    assert '"install", "--id", "Microsoft.WSL"' in bootstrap
    assert 'Start-Process -FilePath "wsl.exe" -ArgumentList @("--install", "--no-distribution") -Verb RunAs' in bootstrap
    assert "function Install-AndResolveDockerDesktop" in bootstrap
    assert '"install", "--id", "Docker.DockerDesktop"' in bootstrap
    assert "Phase 2/5: starting Docker Desktop and requiring a working Linux-container engine" in bootstrap
    assert "Corvus will not proceed until docker info succeeds" in bootstrap
    assert bootstrap.index("Start-AndWaitDockerDesktop -TimeoutSeconds 240") < bootstrap.index('The embedded application checkout is ready')
    assert "function Repair-IncompleteInstall" in bootstrap
    assert 'Invoke-External "docker" @("compose", "-f", $composePath, "down", "--remove-orphans") -IgnoreExitCode' in bootstrap
    assert 'Remove-Item -LiteralPath $appRoot -Recurse -Force' in bootstrap
    assert "Persistent Docker data volumes were preserved" in bootstrap
    assert bootstrap.index("Repair-IncompleteInstall") < bootstrap.index('The embedded application checkout is ready')
    assert "Installer log:" in bootstrap
    assert "$env:ProgramW6432" in bootstrap
    assert 'Programs\\DockerDesktop\\Docker Desktop.exe' in bootstrap
    assert 'Programs\\DockerDesktop\\resources\\bin' in bootstrap
    assert "Start-Process -FilePath $desktopExecutable" in bootstrap
    assert "Open Docker Desktop manually and wait until it shows that the engine is running" in bootstrap
    assert "Start-AndWaitDockerDesktop" in workbench
    assert "$env:ProgramW6432" in workbench
    assert "function Repair-UpdateProtocol" in workbench
    assert "function Refresh-WindowsShortcuts" in workbench
    assert "[Environment+SpecialFolder]::DesktopDirectory" in workbench
    assert "[Environment+SpecialFolder]::Programs" in workbench
    assert 'New-Item -ItemType Directory -Path $desktop -Force' in workbench
    assert 'New-Item -ItemType Directory -Path $startMenu -Force' in workbench
    assert '$shortcutPath = Join-Path $target "Corvus.lnk"' in workbench
    assert "shortcut creation reported success" in workbench
    assert "Desktop shortcut verified" in workbench
    assert "Start Menu shortcut verified" in workbench
    assert "Refresh-WindowsShortcuts" in update_block
    assert 'CreateShortcut((Join-Path $target "Corvus Sock Browser.url"))' not in workbench
    assert 'Remove-Item -LiteralPath (Join-Path $target "Corvus Sock Browser.url")' in workbench
    assert '$shortcut.IconLocation = "$icon,0"' in workbench
    assert "New-OSINTWorkbenchIcon.ps1" in workbench
    assert '$updateShortcutPath = Join-Path $startMenu "Corvus Update.lnk"' in workbench
    assert 'Remove-Item -LiteralPath (Join-Path $startMenu "Check for Updates.lnk")' in workbench
    assert '$updateShortcut.Arguments = "corvus-update://run"' in workbench
    assert '"shortcuts" { Refresh-WindowsShortcuts }' in workbench
    assert '& "$root\\OSINTWorkbench.ps1" -Command shortcuts' in updater
    assert 'Join-Path $Root "Register-OSINTUpdateProtocol.ps1"' in workbench
    assert 'foreach ($scheme in @("corvus-update", "osint-workbench-update"))' in protocol_registration
    assert '"HKCU:\\Software\\Classes\\$scheme"' in protocol_registration
    assert 'Join-Path $Root "Update-Corvus.ps1"' in protocol_registration
    assert 'Join-Path $Root "Update-CorvusGUI.ps1"' in protocol_registration
    assert 'Join-Path $installRoot "installer\\Update-CorvusBootstrap.ps1"' in protocol_registration
    assert "Update-CorvusBootstrap.ps1" in nsis
    assert '"install", "--id", "Git.Git"' in update_bootstrap
    assert "function Resolve-AppRoot" in update_bootstrap
    assert 'Start-Process -FilePath $powershell' in update_bootstrap
    assert '"URL Protocol"' in protocol_registration
    assert "-WindowStyle Hidden" in protocol_registration
    assert 'Corvus Update.lnk"' in nsis
    assert '"$WINDIR\\explorer.exe" "corvus-update://run"' in nsis
    assert "function Invoke-ComposeWithRetry" in workbench
    assert 'Invoke-ComposeWithRetry -Arguments @("build")' in workbench
    assert 'Invoke-ComposeWithRetry -Arguments @("up", "-d")' in workbench
    assert "function Ensure-ExternalVolumes" in workbench
    assert 'Invoke-Docker -Arguments @("volume", "create", $volume)' in workbench
    invoke_docker_block = workbench.split("function Invoke-Docker", 1)[1].split("function Invoke-Compose", 1)[0]
    assert '$ErrorActionPreference = "Continue"' in invoke_docker_block
    assert '$ErrorActionPreference = $previousPreference' in invoke_docker_block
    assert 'Invoke-Docker -Arguments @("volume", "inspect", $volume) -Capture -IgnoreExitCode' in workbench
    assert "Ensure-ExternalVolumes" in workbench.split("function Install-Workbench", 1)[1]
    assert "Docker Compose failed on attempt" in workbench
    assert "function Write-ComposeDiagnostics" in workbench
    assert '"logs", "--no-color", "--tail", "120"' in workbench
    assert "$exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace" in workbench
    assert "[switch]$PurgeData" in uninstaller
    assert '"compose", "down", "--remove-orphans", "--volumes", "--rmi", "all"' in uninstaller
    assert '@("corvus", "osint-workbench")' in uninstaller
    assert "osint-workbench_workbench-data" in uninstaller
    assert "osint-workbench_browser-profile" in uninstaller
    assert "osint-workbench_browser-evidence" in uninstaller
    assert "corvus-subfinder:latest" in uninstaller
    assert "osint-workbench-subfinder:latest" in uninstaller
    assert "corvus-searxng:latest" in uninstaller
    assert "osint-workbench-searxng:latest" in uninstaller
    assert "docker builder prune" not in uninstaller
    assert "docker system prune" not in uninstaller
    assert "Factory Reset Corvus.lnk" in nsis
    assert "YES — FULL FACTORY RESET" in nsis
    assert '"/PURGE"' in nsis
    assert "git bundle create $bundlePath main" in builder
    assert "git rev-parse --is-shallow-repository" in builder
    assert "git clone --quiet --branch main $bundlePath $bundleProbe" in builder
    assert '$definePrefix = if ($env:OS -eq "Windows_NT") { "/D" } else { "-D" }' in builder
    assert "PRODUCT_FILE_VERSION=$fileVersion" in builder
    assert "Get-FileHash -LiteralPath $installerPath -Algorithm SHA256" in builder
    assert "RELEASE_NOTES.md" in builder
    assert '[string]$Version = "0.2.0-rc13"' in builder
    assert "function Install-AndResolveGitForWindows" in bootstrap
    assert '"install", "--id", "Git.Git"' in bootstrap
    assert '"--scope", "user"' in bootstrap
    assert '"--accept-source-agreements"' in bootstrap
    assert '"--accept-package-agreements"' in bootstrap
    assert '"--disable-interactivity"' in bootstrap
    assert "Test-GitCredentialManager" in bootstrap
    assert "Add-DirectoryToUserPath" in bootstrap
    assert "Continuing setup" in bootstrap
    assert "[switch]$Stream" in bootstrap
    assert 'Invoke-External "powershell.exe" $installArguments -Stream' in bootstrap
    assert '"--disable-interactivity"\n    ) -Stream' in bootstrap
    assert 'Join-Path $logRoot "bootstrap.pid"' in bootstrap
    assert "Remove-Item -LiteralPath $pidPath" in bootstrap
    assert "MUI_CUSTOMFUNCTION_ABORT StopBootstrapOnAbort" in nsis
    assert "Function StopBootstrapOnAbort" in nsis
    assert "EnableWindow $2 1" in nsis
    assert "taskkill.exe" in nsis
    assert "PRODUCT_ICON" in nsis
    assert "OSINTWorkbench.ico" in nsis
    assert 'CreateShortCut "$DESKTOP\\Corvus.lnk" "$WINDIR\\explorer.exe" "$WorkbenchUrl" "$INSTDIR\\installer\\OSINTWorkbench.ico"' in nsis
    assert 'CreateShortCut "$SMPROGRAMS\\Corvus\\Corvus.lnk" "$WINDIR\\explorer.exe" "$WorkbenchUrl" "$INSTDIR\\installer\\OSINTWorkbench.ico"' in nsis
    assert 'DisplayIcon" "$INSTDIR\\installer\\OSINTWorkbench.ico"' in nsis
    assert 'CreateShortCut "$DESKTOP\\Corvus.lnk"' in nsis
    assert 'CreateShortCut "$SMPROGRAMS\\Corvus\\Corvus.lnk"' in nsis
    assert 'Join-Path $brandingDirectory "Corvus.ico"' in builder
    assert "PRODUCT_ICON=$iconPath" in builder
    assert "BRANDING_DIR=$brandingDirectory" in builder
    assert '!define PRODUCT_PUBLISHER "Corvus"' in nsis
    assert 'VIAddVersionKey /LANG=1033 "LegalCopyright" "${PRODUCT_COPYRIGHT}"' in nsis
    assert "MUI_WELCOMEFINISHPAGE_BITMAP" in nsis
    assert "MUI_UNWELCOMEFINISHPAGE_BITMAP" in nsis
    assert "MUI_HEADERIMAGE_BITMAP" in nsis
    assert 'Uninstall Corvus.exe" "" "$INSTDIR\\installer\\OSINTWorkbench.ico"' in nsis
    assert '"corvus-update://run" "$INSTDIR\\installer\\OSINTWorkbench.ico"' in nsis
    assert 'app\\static\\corvus-mark.jpg' in icon_generator
    assert "ReadAllBytes" in icon_generator
    assert "complete JPEG payload" in icon_generator
    assert "FromBase64String" not in icon_generator
    assert "Generated Corvus icon from approved artwork" in icon_generator
    assert "return ,$stream.ToArray()" in icon_generator
    assert '$sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256)' in icon_generator
    assert 'save(destination, format="ICO"' in portable_icon_generator
    assert "corvus-mark.jpg" in portable_icon_generator


def test_macos_installer_creates_native_launcher_daily_update_and_full_uninstaller() -> None:
    root = Path(__file__).resolve().parents[2]
    installer_script = (root / "installer" / "macos" / "install-osint-workbench.sh").read_text(
        encoding="utf-8"
    )
    common = (root / "installer" / "macos" / "common.sh").read_text(encoding="utf-8")
    launcher = (root / "installer" / "macos" / "launch-osint-workbench.sh").read_text(
        encoding="utf-8"
    )
    updater = (root / "installer" / "macos" / "update-osint-workbench.sh").read_text(
        encoding="utf-8"
    )
    uninstaller = (root / "installer" / "macos" / "uninstall-osint-workbench.sh").read_text(
        encoding="utf-8"
    )
    builder = (root / "Build-macOS-Package.sh").read_text(encoding="utf-8")

    assert "Application Support/OSINT Workbench" in common
    assert "open -gj -a Docker" in common
    assert "docker compose version" in common
    assert "Library/LaunchAgents/ie.osintworkbench.daily-update.plist" in common
    assert "validate_repository_url" in common
    assert "git clone --branch main --single-branch" in installer_script
    assert "scripts/installer.py install" in installer_script
    assert "CFBundleIdentifier" in installer_script
    assert "CFBundleIconFile" in installer_script
    assert "OSINTWorkbench.icns" in installer_script
    assert "qlmanage -t -s 1024" in installer_script
    assert "iconutil -c icns" in installer_script
    assert "Corvus Update.command" in common
    assert "Factory Reset OSINT Workbench.command" in common
    assert "StartCalendarInterval" in installer_script
    assert "<integer>4</integer>" in installer_script
    assert "wait_for_stack" in launcher
    assert "scripts/installer.py update --only-if-changed" in updater
    assert "GIT_TERMINAL_PROMPT=0" in updater
    assert "start_and_wait_docker" in uninstaller
    assert "--volumes --rmi all" in uninstaller
    assert "FULL FACTORY RESET" in uninstaller
    assert "Docker Desktop is required to stop and remove" in uninstaller
    assert "docker system prune" not in uninstaller
    assert "docker builder prune" not in uninstaller
    assert "git bundle create" in builder
    assert "Version must contain only letters" in builder
    assert "OSINT-Workbench-$VERSION-macOS" in builder
    assert "shasum -a 256" in builder


def test_dashboard_uses_sage_green_design_tokens() -> None:
    root = Path(__file__).resolve().parents[2]
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")
    results_css = (root / "app" / "static" / "results.css").read_text(encoding="utf-8")
    report = (root / "app" / "templates" / "report.html").read_text(encoding="utf-8")
    base = (root / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    encoded_mark = root / "app" / "static" / "corvus-mark.jpg.b64"
    readme_mark = root / "app" / "static" / "corvus-mark.jpg"
    materialiser = (root / "scripts" / "materialise_corvus_brand.py").read_text(encoding="utf-8")

    assert encoded_mark.is_file()
    assert readme_mark.read_bytes().startswith(b"\xff\xd8\xff")
    assert "corvus-mark.jpg.b64" in materialiser
    assert "corvus-mark.jpg" in materialiser
    assert 'rel="icon" href="/static/corvus-mark.jpg?v=corvus-raster-2"' in base
    assert 'class="brand-mark" src="/static/corvus-mark.jpg?v=corvus-raster-2"' in base
    assert 'width="85" height="85"' in base
    assert ".brand-mark{display:block;width:85px;height:85px" in css
    assert ".brand-symbol{display:block;width:100px;height:100px" in report
    assert ".mini-mark{display:block;width:75px;height:75px" in report
    assert ".toolbar-brand .mini-mark{width:75px;height:75px" in report
    assert readme.startswith("# Corvus\n")
    assert 'src="app/static/corvus-mark.jpg"' in readme
    assert "OSINT Workbench — local investigation console" not in readme
    assert "--accent:#5e7458" in css
    assert "--accent-strong:#4b6147" in css
    assert "--accent:#a5c19d" in css
    assert "#155eef" not in css
    assert "#173a7c" not in css
    assert "var(--blue)" not in results_css
    assert "--brand:#294f39" in report
    assert "--brand-deep:#173526" in report
    assert "--blue" not in report
    assert "Corvus" in report
    assert "Document purpose" in report
    assert ".dashboard-hero-card" in css
    assert ".shortcut-menu" in css


def test_corvus_update_gui_uses_branded_wpf_surface() -> None:
    root = Path(__file__).resolve().parents[2]
    gui = (root / "Update-CorvusGUI.ps1").read_text(encoding="utf-8")

    assert "PresentationFramework" in gui
    assert "System.Windows.Forms" not in gui
    assert 'Title="Corvus Update"' in gui
    assert 'ResizeMode="CanResizeWithGrip"' in gui
    assert 'Background="#0B0F0C"' in gui
    assert 'CornerRadius="14"' in gui
    assert "Run Corvus Update" in gui
    assert "Checking for updates" in gui
    assert "Update details" in gui
    assert "Technical details" in gui
    assert "Show-Details" in gui
    assert '"Update-Corvus.ps1"' in gui
    assert 'runtime\\last-update-changes.txt' in gui
    assert '.pending-update-changes.txt' in gui
    assert 'CHANGELOG.md' in gui
    assert '## Unreleased' in gui
    assert 'Current Corvus changes:' in gui
    assert "$null -ne $exitCode -and $exitCode -eq 0" in gui
    assert 'Text="What\'s new"' in gui
    assert 'Text="last installed update"' in gui
    assert 'x:Name="ChangeLogBox"' in gui
    assert 'Join-Path $root ".last-update-changes.txt"' in gui
    assert "function Refresh-ChangeLog" in gui


def test_corvus_update_wrapper_uses_named_parameter_splatting_and_propagates_failure() -> None:
    root = Path(__file__).resolve().parents[2]
    wrapper = (root / "Update-Corvus.ps1").read_text(encoding="utf-8")

    assert "$forward = @{}" in wrapper
    assert '$forward["OnlyIfChanged"] = $true' in wrapper
    assert '$forward["RepositoryUrl"] = $RepositoryUrl' in wrapper
    assert "& $legacyUpdater @forward" in wrapper
    assert "$arguments = @()" not in wrapper
    assert "exit $LASTEXITCODE" not in wrapper
    assert "if (-not $?) { exit 1 }" in wrapper
    assert "Write-Error $_" in wrapper


def test_windows_installer_registers_daily_change_aware_update() -> None:
    root = Path(__file__).resolve().parents[2]
    installer_script = (root / "Install-OSINTWorkbench.ps1").read_text(encoding="utf-8")
    updater = (root / "Update-OSINTWorkbench.ps1").read_text(encoding="utf-8")
    scheduler = (root / "Register-OSINTDailyUpdate.ps1").read_text(encoding="utf-8")
    protocol_registration = (root / "Register-OSINTUpdateProtocol.ps1").read_text(encoding="utf-8")
    github_https = "https://github.com/sn0ba11/corvus.git"
    assert '[string]$DailyUpdateTime = "04:00"' in installer_script
    assert "$DefaultRepositoryUrl" not in installer_script
    assert "Register-OSINTDailyUpdate" in installer_script
    assert 'Write-Host "Corvus Sock Browser"' in installer_script
    assert "VNC password" not in installer_script
    assert "[string]$RepositoryUrl" in scheduler
    assert f'[string]$RepositoryUrl = "{github_https}"' not in scheduler
    assert "New-ScheduledTaskTrigger -Daily" in scheduler
    assert "-StartWhenAvailable" in scheduler
    assert "-NonInteractive -OnlyIfChanged -ScheduledRun" in scheduler
    assert '"Corvus Daily Update"' in scheduler
    assert "if (-not $ScheduledRun)" in updater
    assert "Register-OSINTDailyUpdate -Root $root" in updater
    assert "function Register-UpdateProtocol" in updater
    assert 'Join-Path $root "Register-OSINTUpdateProtocol.ps1"' in updater
    assert 'foreach ($scheme in @("corvus-update", "osint-workbench-update"))' in protocol_registration
    assert '"HKCU:\\Software\\Classes\\$scheme"' in protocol_registration
    assert "Update-CorvusGUI.ps1" in protocol_registration
    assert (
        "Register-OSINTDailyUpdate -Root $root -RepositoryUrl $DefaultRepositoryUrl" not in updater
    )
    assert "Get-ScheduledTask -TaskName" not in updater


def _source_fake_run(calls: list[list[str]], origin: str):  # type: ignore[no-untyped-def]
    commit = "a" * 40

    def fake_run(args: list[str], **kwargs) -> str:  # type: ignore[no-untyped-def]
        calls.append(args)
        if args == ["git", "branch", "--show-current"]:
            return "main"
        if args[:2] == ["git", "rev-list"]:
            return "0 1"
        if args == ["git", "remote", "get-url", "origin"]:
            return origin
        if args[:2] == ["git", "rev-parse"]:
            return commit
        return ""

    return fake_run


@pytest.mark.parametrize("check_only", [True, False])
def test_supplied_address_is_saved_by_update_but_not_by_check(
    tmp_path: Path, monkeypatch, check_only: bool, capsys
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/git")
    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "run", _source_fake_run(calls, "https://old.example/corvus.git"))
    monkeypatch.setattr(installer, "_stage_update_changelog", lambda *_: None)
    new = "https://new.example/corvus.git"
    try:
        installer.update_source(new, check_only=check_only)
    except SystemExit:
        pass  # the fake run reports HEAD != fetched ref after a merge; only the calls matter
    set_url = ["git", "remote", "set-url", "origin", new]
    assert (set_url in calls) is (not check_only)
    if check_only:
        assert ["git", "fetch", new, "refs/heads/main:refs/corvus/update-check"] in calls
        assert ["git", "update-ref", "-d", "refs/corvus/update-check"] in calls
        assert not any(c[:2] == ["git", "fetch"] and "origin" in c for c in calls)
        assert json.loads(capsys.readouterr().out)["source"] == new


def test_check_with_new_address_leaves_real_origin_and_refs_alone(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import shutil as _shutil
    import subprocess as _subprocess

    git_bin = _shutil.which("git")
    if not git_bin:
        pytest.skip("git is not installed")

    def git(cwd: Path, *a: str) -> str:
        return _subprocess.run(  # noqa: S603
            [git_bin, "-c", "user.name=T", "-c", "user.email=t@example.invalid", *a],
            cwd=cwd, check=True, text=True, capture_output=True,
        ).stdout.strip()

    old, new, checkout = tmp_path / "old", tmp_path / "new", tmp_path / "checkout"
    old.mkdir()
    git(old, "init", "-b", "main")
    (old / "f").write_text("1")
    git(old, "add", ".")
    git(old, "commit", "-m", "one")
    git(tmp_path, "clone", str(old), str(new))
    (new / "f").write_text("2")
    git(new, "commit", "-am", "two")
    git(tmp_path, "clone", str(old), str(checkout))
    # File paths are not valid update addresses, so bypass the validator for this local test.
    monkeypatch.setattr(installer, "repository_url_valid", lambda _: True)
    monkeypatch.setattr(installer, "ROOT", checkout)
    before = git(checkout, "for-each-ref")
    assert installer.update_source(str(new), check_only=True)
    assert git(checkout, "remote", "get-url", "origin") == str(old)
    assert git(checkout, "for-each-ref") == before


def test_current_update_source(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    assert installer.current_update_source() == installer.DEFAULT_REPOSITORY_URL
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(installer, "run", lambda *a, **k: "https://x.example/c.git")
    assert installer.current_update_source() == "https://x.example/c.git"
    monkeypatch.setattr(installer, "run", lambda *a, **k: "")
    assert installer.current_update_source() == installer.DEFAULT_REPOSITORY_URL


def test_gui_update_source_field_and_single_default_address() -> None:
    root = Path(__file__).resolve().parents[2]
    gui = (root / "Update-CorvusGUI.ps1").read_text(encoding="utf-8")
    worker = (root / "Update-OSINTWorkbench.ps1").read_text(encoding="utf-8")
    literal = f'$DefaultRepositoryUrl = "{installer.DEFAULT_REPOSITORY_URL}"'
    assert literal in gui
    assert literal in worker
    for name in ("SourceBox", "ResetSourceButton"):
        assert f'x:Name="{name}"' in gui
        assert f'FindName("{name}")' in gui
    assert "Reset to default" in gui
    assert "Corvus fetches updates from this Git address. Change it only if the source has moved." in gui
    assert '"-RepositoryUrl"' in gui
    assert "remote get-url origin" in gui


def test_repository_address_cannot_be_a_git_option() -> None:
    assert not installer.repository_url_valid("-oProxyCommand=evil@host:path")
    assert not installer.repository_url_valid("--upload-pack=x@host:path")
    assert installer.repository_url_valid("git@git.example:owner/corvus.git")
    assert installer.repository_url_valid("https://github.com/sn0ba11/corvus.git")


def test_windows_installer_branding_assets_match_approved_artwork(tmp_path: Path) -> None:
    from PIL import Image

    root = Path(__file__).resolve().parents[2]
    branding = root / "installer" / "windows" / "branding"
    with Image.open(branding / "Corvus.ico") as icon:
        assert {16, 24, 32, 48, 64, 128, 256} <= {size[0] for size in icon.info["sizes"]}
    for name, size in (("Corvus-wizard.bmp", (164, 314)), ("Corvus-header.bmp", (150, 57))):
        with Image.open(branding / name) as bitmap:
            assert bitmap.format == "BMP" and bitmap.mode == "RGB" and bitmap.size == size

    import importlib.util

    spec = importlib.util.spec_from_file_location("corvus_icon", root / "scripts" / "new_osint_workbench_icon.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_icon(tmp_path / "Corvus.ico")
    assert (tmp_path / "Corvus.ico").read_bytes() == (branding / "Corvus.ico").read_bytes()

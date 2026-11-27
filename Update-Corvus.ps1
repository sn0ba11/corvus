param(
    [switch]$Browser,
    [string]$RepositoryUrl,
    [switch]$SkipSourceUpdate,
    [switch]$NonInteractive,
    [switch]$OnlyIfChanged,
    [switch]$ScheduledRun
)

$ErrorActionPreference = "Stop"
$legacyUpdater = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "Update-OSINTWorkbench.ps1"
if (-not (Test-Path -LiteralPath $legacyUpdater -PathType Leaf)) {
    throw "Corvus update engine was not found at $legacyUpdater"
}

$forward = @{}
if ($Browser) { $forward["Browser"] = $true }
if ($RepositoryUrl) { $forward["RepositoryUrl"] = $RepositoryUrl }
if ($SkipSourceUpdate) { $forward["SkipSourceUpdate"] = $true }
if ($NonInteractive) { $forward["NonInteractive"] = $true }
if ($OnlyIfChanged) { $forward["OnlyIfChanged"] = $true }
if ($ScheduledRun) { $forward["ScheduledRun"] = $true }

try {
    & $legacyUpdater @forward
    if (-not $?) { exit 1 }
    exit 0
} catch {
    Write-Error $_
    exit 1
}

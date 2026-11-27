[CmdletBinding()]
param(
    [string]$InstallRoot,
    [string]$RepositoryUrl,
    [switch]$OnlyIfChanged,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
if (-not $InstallRoot) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Corvus\app'),
        (Join-Path $env:LOCALAPPDATA 'Programs\OSINT Workbench\app')
    ) | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'Update-Corvus.ps1') -PathType Leaf }
    if (@($candidates).Count -ne 1) {
        throw 'Cannot uniquely locate Corvus. Supply -InstallRoot with the existing application checkout path.'
    }
    $InstallRoot = @($candidates)[0]
}
$root = (Resolve-Path -LiteralPath $InstallRoot).Path
$updater = Join-Path $root 'Update-Corvus.ps1'
if (-not (Test-Path -LiteralPath $updater -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
    throw 'InstallRoot must point to the existing Corvus Git checkout (usually the app subdirectory).'
}
# FileShare.None prevents another packaged updater from changing this checkout.
# The OS releases the handle even if the process is terminated.
$runtime = Join-Path $root 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$lockPath = Join-Path $runtime 'corvus-update.lock'
try {
    $updateLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch [IO.IOException] {
    throw 'Another Corvus updater is already running, or the update lock is unavailable.'
}
try {
    $forward = @{}
    if ($OnlyIfChanged) { $forward['OnlyIfChanged'] = $true }
    if ($NonInteractive) { $forward['NonInteractive'] = $true }
    if ($RepositoryUrl) { $forward['RepositoryUrl'] = $RepositoryUrl }
    & $updater @forward
    if (-not $?) { exit 1 }
} finally {
    $updateLock.Dispose()
}

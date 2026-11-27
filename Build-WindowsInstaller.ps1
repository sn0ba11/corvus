param(
    [string]$Version = "0.2.0-rc14",
    [string]$RepositoryUrl = "https://github.com/sn0ba11/corvus.git",
    [string]$OutputDirectory,
    [string]$MakensisPath,
    [switch]$SkipCleanCheck
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $root "dist\release"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$payloadDirectory = Join-Path ([IO.Path]::GetTempPath()) ("corvus-windows-installer-" + [Guid]::NewGuid().ToString("N"))
$bundlePath = Join-Path $payloadDirectory "osint-workbench.bundle"
$brandingDirectory = Join-Path $root "installer\windows\branding"
$iconPath = Join-Path $brandingDirectory "Corvus.ico"
$nsiPath = Join-Path $root "installer\windows\OSINTWorkbench.nsi"

function Find-Makensis {
    if ($MakensisPath) {
        if (-not (Test-Path -LiteralPath $MakensisPath -PathType Leaf)) {
            throw "makensis was not found at '$MakensisPath'."
        }
        return [IO.Path]::GetFullPath($MakensisPath)
    }
    $command = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command makensis -ErrorAction SilentlyContinue }
    if ($command) { return $command.Source }
    foreach ($candidate in @(
        "$env:ProgramFiles\NSIS\makensis.exe",
        "${env:ProgramFiles(x86)}\NSIS\makensis.exe"
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    throw "NSIS is required to build the standard installer. Install NSIS 3 and rerun this script."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required to build the installer."
}
if (-not (Test-Path -LiteralPath $nsiPath -PathType Leaf)) {
    throw "The NSIS installer definition is missing."
}
if ($RepositoryUrl -notmatch '^https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+\.git$') {
    throw "RepositoryUrl must be a credential-free HTTPS Git repository URL."
}
$versionMatch = [regex]::Match($Version, '^(\d+)\.(\d+)\.(\d+)(?:-[A-Za-z0-9.-]+)?$')
if (-not $versionMatch.Success) {
    throw "Version must look like 0.1.0 or 0.1.0-rc1."
}
$fileVersion = "$($versionMatch.Groups[1].Value).$($versionMatch.Groups[2].Value).$($versionMatch.Groups[3].Value).0"

Push-Location $root
try {
    if (-not $SkipCleanCheck) {
        $changes = & git status --porcelain --untracked-files=normal
        if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the Git checkout." }
        if ($changes) { throw "The Git checkout is dirty. Commit or remove changes before packaging." }
    }
    & git rev-parse --verify main | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The main branch is unavailable." }
    $headCommit = (& git rev-parse HEAD).Trim()
    $mainCommit = (& git rev-parse main).Trim()
    if ($headCommit -ne $mainCommit) {
        throw "HEAD must match main before packaging, so embedded source and manifest describe the same commit."
    }

    if ((& git rev-parse --is-shallow-repository).Trim() -ne 'false') {
        throw "Cannot build a release from a shallow Git checkout: the embedded bundle would be missing required history."
    }
    New-Item -ItemType Directory -Path $payloadDirectory, $OutputDirectory -Force | Out-Null
    & git bundle create $bundlePath main
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the embedded Git bundle." }
    & git bundle verify $bundlePath | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "The embedded Git bundle failed verification." }
    # Verify from an empty repository, not the builder checkout: the latter
    # could conceal missing objects by supplying them from its own history.
    $bundleProbe = Join-Path $payloadDirectory "bundle-clone-check"
    try {
        & git clone --quiet --branch main $bundlePath $bundleProbe
        if ($LASTEXITCODE -ne 0) { throw "The embedded Git bundle cannot be cloned independently." }
        $probeCommit = (& git -C $bundleProbe rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $probeCommit -ne $mainCommit) {
            throw "The embedded Git bundle does not reproduce the packaged commit."
        }
    } finally {
        Remove-Item -LiteralPath $bundleProbe -Recurse -Force -ErrorAction SilentlyContinue
    }
    # Committed branding is used on every build host, so Windows and Linux
    # builds embed identical artwork. Regenerate with scripts/new_osint_workbench_icon.py.
    foreach ($asset in @("Corvus.ico", "Corvus-wizard.bmp", "Corvus-header.bmp")) {
        if (-not (Test-Path -LiteralPath (Join-Path $brandingDirectory $asset) -PathType Leaf)) {
            throw "The Corvus branding asset '$asset' is missing from installer\windows\branding."
        }
    }

    $makensis = Find-Makensis
    $definePrefix = if ($env:OS -eq "Windows_NT") { "/D" } else { "-D" }
    $makensisArguments = @(
        "${definePrefix}PRODUCT_VERSION=$Version",
        "${definePrefix}SOURCE_REPOSITORY=$RepositoryUrl",
        "${definePrefix}PRODUCT_FILE_VERSION=$fileVersion",
        "${definePrefix}PRODUCT_ICON=$iconPath",
        "${definePrefix}BRANDING_DIR=$brandingDirectory",
        "${definePrefix}PROJECT_ROOT=$root",
        "${definePrefix}PAYLOAD_DIR=$payloadDirectory",
        "${definePrefix}OUTPUT_DIR=$OutputDirectory"
    )
    $makensisArguments += $nsiPath
    & $makensis @makensisArguments
    if ($LASTEXITCODE -ne 0) { throw "NSIS failed with exit code $LASTEXITCODE." }

    $installerPath = Join-Path $OutputDirectory "Corvus-$Version-Setup.exe"
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "NSIS completed without producing '$installerPath'."
    }
    $hash = Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
    $checksumPath = "$installerPath.sha256"
    [IO.File]::WriteAllText(
        $checksumPath,
        ($hash.Hash.ToLowerInvariant() + "  " + [IO.Path]::GetFileName($installerPath) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
    $commit = (& git rev-parse HEAD).Trim()
    $manifest = [ordered]@{
        product = "Corvus"
        version = $Version
        commit = $commit
        source_repository = $RepositoryUrl
        installer = [IO.Path]::GetFileName($installerPath)
        sha256 = $hash.Hash.ToLowerInvariant()
        built_at_utc = [DateTime]::UtcNow.ToString("o")
        signed = $false
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory "release-manifest.json") -Encoding UTF8
    $releaseNotes = Join-Path $root "CHANGELOG.md"
    if (Test-Path -LiteralPath $releaseNotes -PathType Leaf) {
        Copy-Item -LiteralPath $releaseNotes -Destination (Join-Path $OutputDirectory "RELEASE_NOTES.md") -Force
    }
    Write-Host ""
    Write-Host "Windows installer built successfully:" -ForegroundColor Green
    Write-Host $installerPath
    Write-Host "SHA-256: $($hash.Hash.ToLowerInvariant())"
    Write-Warning "This release candidate is not Authenticode-signed. Windows SmartScreen may display an unknown-publisher warning."
} finally {
    Remove-Item -LiteralPath $payloadDirectory -Recurse -Force -ErrorAction SilentlyContinue
    Pop-Location
}

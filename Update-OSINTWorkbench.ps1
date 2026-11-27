param(
    [switch]$Browser,
    [string]$RepositoryUrl,
    [switch]$SkipSourceUpdate,
    [switch]$NonInteractive,
    [switch]$OnlyIfChanged,
    [switch]$ScheduledRun
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultRepositoryUrl = "https://github.com/sn0ba11/corvus.git"
$UpdateBranch = "main"
$UnknownBuildCommit = "0000000000000000000000000000000000000000"
$transcriptStarted = $false
$updateChangeLog = ""
$pendingChangeLogPath = Join-Path $root ".pending-update-changes.txt"
$installedChangeLogPath = Join-Path $root ".last-update-changes.txt"
$runtimeChangeLogRoot = Join-Path $root "runtime"
$runtimeChangeLogPath = Join-Path $runtimeChangeLogRoot "last-update-changes.txt"
$pendingRangePath = Join-Path $root ".pending-update-range.json"
$installedRangePath = Join-Path $root ".last-update-range.json"
$runtimeRangePath = Join-Path $runtimeChangeLogRoot "last-update-range.json"
if ($ScheduledRun) {
    $installRoot = Split-Path -Parent $root
    $logRoot = if (Test-Path -LiteralPath (Join-Path $installRoot "installer-result.ini")) {
        Join-Path $installRoot "logs"
    } else {
        Join-Path $env:LOCALAPPDATA "Corvus\logs"
    }
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $scheduledLog = Join-Path $logRoot "scheduled-update.log"
    if ((Test-Path -LiteralPath $scheduledLog) -and (Get-Item -LiteralPath $scheduledLog).Length -gt 5MB) {
        Move-Item -LiteralPath $scheduledLog -Destination "$scheduledLog.1" -Force
    }
    try {
        Start-Transcript -Path $scheduledLog -Append | Out-Null
        $transcriptStarted = $true
    } catch {
        # Updating must still continue if transcript creation is unavailable.
    }
}
. "$root\Register-OSINTDailyUpdate.ps1"

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (($machine, $user) | Where-Object { $_ }) -join ";"
}

function Find-GitExecutable {
    Refresh-ProcessPath
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command git -ErrorAction SilentlyContinue }
    if ($command -and $command.Path) { return [IO.Path]::GetFullPath($command.Path) }
    foreach ($candidate in @(
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"),
        (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe" })
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Find-WingetExecutable {
    $command = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Path) { return [IO.Path]::GetFullPath($command.Path) }
    $candidate = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [IO.Path]::GetFullPath($candidate) }
    return $null
}

function Ensure-GitExecutable {
    $gitExecutable = Find-GitExecutable
    if ($gitExecutable) { return $gitExecutable }
    if ($env:OS -ne "Windows_NT") { throw "Git is required to update Corvus and could not be found." }
    $wingetExecutable = Find-WingetExecutable
    if (-not $wingetExecutable) {
        throw "Git for Windows is missing and Windows Package Manager (winget) is unavailable."
    }
    Write-Host "Git for Windows is missing. Installing it automatically..."
    $process = Start-Process -FilePath $wingetExecutable -ArgumentList @(
        "install", "--id", "Git.Git", "--exact", "--source", "winget",
        "--scope", "user", "--silent", "--accept-source-agreements",
        "--accept-package-agreements", "--disable-interactivity"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Git for Windows installation failed with exit code $($process.ExitCode)." }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        $gitExecutable = Find-GitExecutable
        if ($gitExecutable) { return $gitExecutable }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Git for Windows installed but git.exe did not become available within 120 seconds."
}

$script:GitExecutable = $null
function Invoke-Git([string[]]$Arguments, [switch]$Capture, [switch]$IgnoreExitCode) {
    if (-not $script:GitExecutable) { $script:GitExecutable = Ensure-GitExecutable }
    Push-Location $root
    try {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            # The installed checkout can legitimately be owned by the original/domain
            # account while the updater is launched through a local maintenance account.
            # Scope safe.directory to this invocation only; do not weaken global Git policy.
            $gitArguments = @("-c", "safe.directory=$root") + $Arguments
            $output = & $script:GitExecutable @gitArguments 2>&1
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($exitCode -ne 0) {
            if (-not $IgnoreExitCode) {
                throw "git $($Arguments -join ' ') failed with exit code $exitCode.`n$($output -join [Environment]::NewLine)"
            }
            if ($Capture) { return "" }
            return
        }
        if ($Capture) { return ($output -join [Environment]::NewLine).Trim() }
        if ($output) { $output | ForEach-Object { Write-Host $_ } }
    } finally {
        Pop-Location
    }
}

function Register-UpdateProtocol {
    $registrationScript = Join-Path $root "Register-OSINTUpdateProtocol.ps1"
    if (-not (Test-Path -LiteralPath $registrationScript -PathType Leaf)) {
        throw "Update protocol registration script was not found at $registrationScript"
    }
    & $registrationScript -Root $root
}

function Set-Origin([string]$CurrentOrigin, [string]$Selected) {
    if ($CurrentOrigin) {
        if ($CurrentOrigin -ne $Selected) { Invoke-Git @("remote", "set-url", "origin", $Selected) }
    } else {
        Invoke-Git @("remote", "add", "origin", $Selected)
    }
}

function Update-Source {
    if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
        Write-Warning "This is not a Git clone; updating the currently extracted release only."
        return $false
    }
    $script:GitExecutable = Ensure-GitExecutable

    $origin = Invoke-Git @("remote", "get-url", "origin") -Capture -IgnoreExitCode
    $selected = if ($RepositoryUrl) {
        $RepositoryUrl.Trim()
    } elseif ($origin) {
        $origin
    } else {
        $DefaultRepositoryUrl
    }
    if ($selected -notmatch '^(https?|ssh)://[^\s]+$' -and $selected -notmatch '^[^@\s]+@[^:\s]+:.+$') {
        throw "Repository URL must be HTTP(S), SSH, or Git's user@host:path form."
    }

    # Only tracked modifications block an update. Untracked analyst/packaging folders are
    # harmless unless they collide with an incoming tracked path, in which case Git's
    # fast-forward merge fails safely rather than silently overwriting them.
    $changes = Invoke-Git @("status", "--porcelain", "--untracked-files=no") -Capture
    if ($changes) {
        throw "The source checkout has tracked local changes. Commit or remove them before updating."
    }
    if ($NonInteractive -or $ScheduledRun) {
        $env:GIT_TERMINAL_PROMPT = "0"
        $env:GCM_INTERACTIVE = "never"
    }
    $branch = Invoke-Git @("branch", "--show-current") -Capture
    if ($branch -and $branch -ne $UpdateBranch) {
        throw "Updates require branch $UpdateBranch; switch branches explicitly before updating (current: $branch)."
    }
    $previous = Invoke-Git @("rev-parse", "HEAD") -Capture
    $branchRef = "refs/heads/$UpdateBranch"
    $trackingRef = "refs/remotes/origin/$UpdateBranch"
    $refspec = "{0}:{1}" -f $branchRef, $trackingRef
    Set-Origin $origin $selected
    Write-Host "Checking the configured repository for a Corvus update..."
    Invoke-Git @("fetch", "--prune", "origin", $refspec)
    $available = Invoke-Git @("rev-parse", $trackingRef) -Capture
    $counts = (Invoke-Git @("rev-list", "--left-right", "--count", "HEAD...$trackingRef") -Capture) -split "\s+"
    if ([int]$counts[0] -gt 0) {
        throw "The installed commit is not an ancestor of origin/main. Resolve local history before updating; no source was merged."
    }
    if (-not $branch) {
        # Packaged installations can be left detached after an installer upgrade.
        # Attach only when the existing main branch is exactly the installed commit;
        # never overwrite a branch or discard local history to repair an install.
        $localMain = Invoke-Git @("rev-parse", "--verify", "refs/heads/$UpdateBranch") -Capture -IgnoreExitCode
        if ($localMain -and $localMain -ne $previous) {
            throw "Detached HEAD at $previous differs from local $UpdateBranch at $localMain. Refusing automatic branch repair; no source was merged."
        }
        if ($localMain) {
            Invoke-Git @("switch", $UpdateBranch)
        } else {
            Invoke-Git @("switch", "-c", $UpdateBranch)
        }
        $attached = Invoke-Git @("rev-parse", "HEAD") -Capture
        if ((Invoke-Git @("branch", "--show-current") -Capture) -ne $UpdateBranch -or $attached -ne $previous) {
            throw "Failed to attach the packaged checkout to $UpdateBranch without changing its installed commit."
        }
        Write-Host "Repaired packaged checkout: attached detached HEAD to $UpdateBranch without changing files."
    }
    Write-Host "Installed commit: $($previous.Substring(0, 8))"
    Write-Host "Available commit: $($available.Substring(0, 8))"
    Invoke-Git @("config", "branch.$UpdateBranch.remote", "origin")
    Invoke-Git @("config", "branch.$UpdateBranch.merge", $branchRef)
    if ($previous -eq $available) {
        Write-Host "Corvus is already current."
        return $false
    }
    Invoke-Git @("merge", "--ff-only", $trackingRef)
    $current = Invoke-Git @("rev-parse", "HEAD") -Capture
    if ($current -ne $available) {
        throw "Source update verification failed: HEAD is $current but fetched $trackingRef is $available."
    }
    Write-Host "Source updated to: $($current.Substring(0, 8))"
    $changes = Invoke-Git @(
        "log", "--no-merges", "--date=short", "--pretty=format:@@CHANGE@@%n%ad%n%H%n%s%n%b%n@@END@@", "$previous..$current"
    ) -Capture -IgnoreExitCode
    if (-not $changes) {
        $changes = "Updated source from $($previous.Substring(0, [Math]::Min(12, $previous.Length))) to $($current.Substring(0, [Math]::Min(12, $current.Length)))."
    }
    $script:updateChangeLog = "Corvus changes installed:`r`n$changes"
    Set-Content -LiteralPath $pendingChangeLogPath -Value $script:updateChangeLog -Encoding UTF8
    [ordered]@{
        from_commit = $previous
        to_commit = $current
        fallback_used = $false
    } | ConvertTo-Json | Set-Content -LiteralPath $pendingRangePath -Encoding UTF8
    return $true
}

$sourceChanged = $false
if (-not $SkipSourceUpdate) { $sourceChanged = Update-Source }
$currentHead = Invoke-Git @("rev-parse", "HEAD") -Capture
$envBuildCommit = ""
$envPath = Join-Path $root ".env"
if (Test-Path -LiteralPath $envPath -PathType Leaf) {
    $envBuildLine = Get-Content -LiteralPath $envPath | Where-Object { $_.StartsWith("OSINT_BUILD_COMMIT=") } | Select-Object -First 1
    if ($envBuildLine) { $envBuildCommit = $envBuildLine.Substring("OSINT_BUILD_COMMIT=".Length).Trim().ToLowerInvariant() }
}
$appMetadataStale = $currentHead -match '^[0-9a-fA-F]{40}$' -and $envBuildCommit -ne $currentHead.ToLowerInvariant()
if ($appMetadataStale) {
    Write-Host "Running build metadata is stale ($envBuildCommit); Git HEAD is $($currentHead.Substring(0, 8)). Forcing app rebuild."
}
if ($OnlyIfChanged -and -not $sourceChanged -and -not $appMetadataStale -and -not (Test-Path -LiteralPath $pendingChangeLogPath -PathType Leaf) -and -not (Test-Path -LiteralPath $pendingRangePath -PathType Leaf)) {
    if ($env:OS -eq "Windows_NT") {
        & "$root\OSINTWorkbench.ps1" -Command shortcuts
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    exit 0
}

& "$root\OSINTWorkbench.ps1" -Command update -Browser:$Browser -ForceAppRebuild:($sourceChanged -or $appMetadataStale) -BuildCommitOverride $currentHead
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

New-Item -ItemType Directory -Path $runtimeChangeLogRoot -Force | Out-Null
if (Test-Path -LiteralPath $pendingChangeLogPath -PathType Leaf) {
    $updateChangeLog = (Get-Content -LiteralPath $pendingChangeLogPath -Raw).TrimEnd()
    Move-Item -LiteralPath $pendingChangeLogPath -Destination $installedChangeLogPath -Force
    Copy-Item -LiteralPath $installedChangeLogPath -Destination $runtimeChangeLogPath -Force
}
if (Test-Path -LiteralPath $pendingRangePath -PathType Leaf) {
    Move-Item -LiteralPath $pendingRangePath -Destination $installedRangePath -Force
    Copy-Item -LiteralPath $installedRangePath -Destination $runtimeRangePath -Force
}
if ($updateChangeLog) {
    Write-Host ""
    Write-Host $updateChangeLog
    Write-Host ""
}

if (-not $ScheduledRun) {
    try {
        Register-UpdateProtocol
    } catch {
        Write-Warning "The update succeeded, but the dashboard update handler could not be registered: $($_.Exception.Message)"
    }
    try {
        Register-OSINTDailyUpdate -Root $root
    } catch {
        Write-Warning "The update succeeded, but the daily update task could not be refreshed: $($_.Exception.Message)"
    }
}

if ($transcriptStarted) {
    Stop-Transcript | Out-Null
}

param(
    [Parameter(Mandatory = $true)]
    [string]$BundlePath,
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [string]$RepositoryUrl = "https://github.com/sn0ba11/corvus.git",
    [string]$DailyUpdateTime = "04:00",
    [switch]$NoDailyUpdate
)

$ErrorActionPreference = "Stop"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$BundlePath = [IO.Path]::GetFullPath($BundlePath)
$appRoot = Join-Path $InstallRoot "app"
$logRoot = Join-Path $InstallRoot "logs"
$resultPath = Join-Path $InstallRoot "installer-result.ini"
$pidPath = Join-Path $logRoot "bootstrap.pid"
New-Item -ItemType Directory -Path $InstallRoot, $logRoot -Force | Out-Null
[IO.File]::WriteAllText($pidPath, [string]$PID, (New-Object Text.UTF8Encoding($false)))
$transcriptStarted = $false

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$Capture,
        [switch]$Stream,
        [switch]$IgnoreExitCode
    )
    if ($Capture -and $Stream) {
        throw "Invoke-External cannot capture and stream the same command."
    }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Stream) {
            $output = @()
            & $Executable @Arguments 2>&1 | ForEach-Object {
                Write-Host ([string]$_)
            }
            $exitCode = $LASTEXITCODE
        } else {
            $output = & $Executable @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        if (-not $IgnoreExitCode) {
            throw "$Executable $($Arguments -join ' ') failed with exit code $exitCode.`n$($output -join [Environment]::NewLine)"
        }
        if ($Capture) { return "" }
        return
    }
    if ($Capture) { return ($output -join [Environment]::NewLine).Trim() }
    if ($output) { $output | ForEach-Object { Write-Host $_ } }
}

function Update-AppCheckoutFromBundle {
    param(
        [Parameter(Mandatory = $true)][string]$GitExecutable,
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$BundlePath
    )
    # refs/remotes/installer/main belongs to the installer, so it may be
    # replaced outright ("+"); a previous release need not be its ancestor.
    Invoke-External $GitExecutable @(
        "-C", $AppRoot, "fetch", $BundlePath,
        "+refs/heads/main:refs/remotes/installer/main"
    )
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $GitExecutable -C $AppRoot merge-base --is-ancestor refs/remotes/installer/main HEAD 2>$null
        $bundleIsAncestor = $LASTEXITCODE -eq 0
        & $GitExecutable -C $AppRoot merge-base --is-ancestor HEAD refs/remotes/installer/main 2>$null
        $headIsAncestor = $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($bundleIsAncestor) { return }
    if ($headIsAncestor) {
        Invoke-External $GitExecutable @("-C", $AppRoot, "merge", "--ff-only", "refs/remotes/installer/main")
        return
    }
    # The checkout comes from a different history, such as a build made before
    # the public release. Keep that history under a backup branch, then move
    # main to the release. Ignored files (.env, local data) are not touched.
    $previousHead = Invoke-External $GitExecutable @("-C", $AppRoot, "rev-parse", "HEAD") -Capture
    $backupBranch = "corvus-pre-migration-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Invoke-External $GitExecutable @("-C", $AppRoot, "branch", "--force", $backupBranch, $previousHead)
    Invoke-External $GitExecutable @("-C", $AppRoot, "checkout", "--force", "-B", "main", "refs/remotes/installer/main")
    Invoke-External $GitExecutable @("-C", $AppRoot, "reset", "--hard", "refs/remotes/installer/main")
    # The updater fetches main into origin/main without force, so a tracking
    # ref left on the earlier history would block every later update.
    Invoke-External $GitExecutable @("-C", $AppRoot, "update-ref", "refs/remotes/origin/main", "refs/remotes/installer/main")
    Write-Host "The existing checkout used a different source history. It moved to the Corvus release; the previous version ($previousHead) is kept in Git branch $backupBranch."
}

function Assert-Command([string]$Name, [string]$InstallMessage) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $InstallMessage"
    }
}

function Convert-ToFriendlyInstallError([string]$RawMessage) {
    $message = ([string]$RawMessage).Trim()
    if (-not $message) { return "Corvus setup stopped because an unknown error occurred. See install.log for details." }

    if ($message -match 'no such volume|external volume .* not found') {
        return "Corvus could not prepare its persistent Docker storage. Setup should create this automatically. Close Docker Desktop, start it again, and rerun this installer. If it repeats, send install.log with the report."
    }
    if ($message -match 'Docker Desktop.*Linux-container.*(unresponsive|not running|available)|docker info') {
        return "Docker Desktop is installed, but its Linux container engine is not running. Open Docker Desktop and wait until it says the engine is running, then rerun Corvus Setup."
    }
    if ($message -match 'WSL 2 .*not ready|Restart Windows') {
        return "Windows has installed or enabled WSL 2, but a restart is required before Docker Desktop can run. Restart Windows, then run this Corvus installer again."
    }
    if ($message -match 'Docker Desktop.*(missing|not found)|docker.exe is not available') {
        return "Docker Desktop could not be installed or found. Make sure Windows Package Manager (winget) is available, then rerun Corvus Setup. See install.log for the installation detail."
    }
    if ($message -match 'Git .*missing|git.exe did not become available|Git Credential Manager') {
        return "Git for Windows could not be prepared automatically. Make sure Windows Package Manager (winget) is available, then rerun Corvus Setup. See install.log for the exact Git error."
    }
    if ($message -match 'health check|did not become healthy|unhealthy') {
        return "Corvus was installed and its containers started, but one or more services did not become healthy. Setup has stopped rather than leaving a partly working installation. See install.log for the affected service and its last log lines."
    }
    if ($message -match 'docker compose .*failed|Docker Compose failed|powershell\.exe .*Install-OSINTWorkbench.*failed') {
        return "Corvus could not start its Docker services. Setup has stopped and saved the technical details to install.log. Restart Docker Desktop and rerun Setup; if it repeats, send install.log with the report."
    }
    return "Corvus setup stopped: $message"
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
}

function Add-DirectoryToUserPath([string]$Directory) {
    $fullDirectory = [IO.Path]::GetFullPath($Directory).TrimEnd("\\")
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $alreadyPresent = $false
    foreach ($entry in @($current -split ";")) {
        if ($entry -and [string]::Equals(
            $entry.Trim().TrimEnd("\\"),
            $fullDirectory,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            $alreadyPresent = $true
            break
        }
    }
    if (-not $alreadyPresent) {
        $updated = if ([string]::IsNullOrWhiteSpace($current)) {
            $fullDirectory
        } else {
            $current.TrimEnd(";") + ";" + $fullDirectory
        }
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
    }
    Refresh-ProcessPath
}

function Find-GitExecutable {
    Refresh-ProcessPath
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command git -ErrorAction SilentlyContinue }
    if ($command -and $command.Path) {
        return [IO.Path]::GetFullPath($command.Path)
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"),
        (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe" })
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Find-WingetExecutable {
    $command = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Path) {
        return [IO.Path]::GetFullPath($command.Path)
    }
    $candidate = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\winget.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return [IO.Path]::GetFullPath($candidate)
    }
    return $null
}

function Test-GitCredentialManager([string]$GitExecutable) {
    $version = Invoke-External $GitExecutable @("credential-manager", "--version") `
        -Capture -IgnoreExitCode
    if ([string]::IsNullOrWhiteSpace($version)) {
        $version = Invoke-External $GitExecutable @("credential-manager-core", "--version") `
            -Capture -IgnoreExitCode
    }
    return -not [string]::IsNullOrWhiteSpace($version)
}

function Install-AndResolveGitForWindows {
    $gitExecutable = Find-GitExecutable
    if ($gitExecutable) {
        if (-not (Test-GitCredentialManager $gitExecutable)) {
            throw "The existing Git installation does not include Git Credential Manager. Install the current Git for Windows with its default components and retry setup."
        }
        Add-DirectoryToUserPath ([IO.Path]::GetDirectoryName($gitExecutable))
        return $gitExecutable
    }

    $wingetExecutable = Find-WingetExecutable
    if (-not $wingetExecutable) {
        throw "Git is missing and Windows Package Manager (winget) is unavailable. Install the current Git for Windows with its default Git Credential Manager component, then retry setup."
    }

    Write-Host "Git for Windows is not installed. Installing it for the current user..."
    Invoke-External $wingetExecutable @(
        "install", "--id", "Git.Git", "--exact", "--source", "winget",
        "--scope", "user", "--silent", "--accept-source-agreements",
        "--accept-package-agreements", "--disable-interactivity"
    ) -Stream

    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        Refresh-ProcessPath
        $gitExecutable = Find-GitExecutable
        if ($gitExecutable) { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)

    if (-not $gitExecutable) {
        throw "Git for Windows reported a successful installation but git.exe did not become available within 120 seconds. Sign out and back in, then retry setup."
    }
    if (-not (Test-GitCredentialManager $gitExecutable)) {
        throw "Git for Windows was installed without Git Credential Manager. Repair the Git installation with its default components and retry setup."
    }
    Add-DirectoryToUserPath ([IO.Path]::GetDirectoryName($gitExecutable))
    Write-Host "Git for Windows is installed and ready. Continuing setup..."
    return $gitExecutable
}

function Test-WslReady {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) { return $false }
    $status = Invoke-External $wsl.Source @("--status") -Capture -IgnoreExitCode
    return -not [string]::IsNullOrWhiteSpace($status)
}

function Install-AndEnsureWsl2 {
    if (Test-WslReady) {
        Invoke-External "wsl.exe" @("--set-default-version", "2") -Capture -IgnoreExitCode | Out-Null
        Write-Host "WSL is installed and available."
        return
    }

    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        $wingetExecutable = Find-WingetExecutable
        if (-not $wingetExecutable) {
            throw "WSL is missing and Windows Package Manager (winget) is unavailable. Install WSL 2, reboot if Windows requests it, and rerun Corvus Setup."
        }
        Write-Host "WSL is not installed. Installing the Microsoft WSL package with administrator approval..."
        $wingetArgs = @(
            "install", "--id", "Microsoft.WSL", "--exact", "--source", "winget",
            "--silent", "--accept-source-agreements", "--accept-package-agreements",
            "--disable-interactivity"
        )
        $wingetProcess = Start-Process -FilePath $wingetExecutable -ArgumentList $wingetArgs -Verb RunAs -Wait -PassThru
        if ($wingetProcess.ExitCode -ne 0) {
            throw "WSL package installation failed with exit code $($wingetProcess.ExitCode). Approve the administrator prompt and retry setup."
        }
        Refresh-ProcessPath
        $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    }

    if (-not (Test-WslReady)) {
        Write-Host "Enabling the Windows components required by WSL 2. Windows may request administrator approval..."
        $process = Start-Process -FilePath "wsl.exe" -ArgumentList @("--install", "--no-distribution") -Verb RunAs -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "WSL 2 setup failed with exit code $($process.ExitCode). Complete WSL installation in Windows and rerun Corvus Setup."
        }
    }

    if (-not (Test-WslReady)) {
        throw "WSL 2 was installed/enabled but is not ready in this Windows session. Restart Windows, then rerun Corvus Setup; Corvus application files have not been installed yet."
    }
    Invoke-External "wsl.exe" @("--set-default-version", "2") -Capture -IgnoreExitCode | Out-Null
    Write-Host "WSL 2 is installed and ready."
}

function Find-DockerDesktopExecutable {
    $candidates = New-Object Collections.Generic.List[string]
    foreach ($registryPath in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Docker Desktop.exe",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Docker Desktop.exe"
    )) {
        try {
            $registered = (Get-Item -LiteralPath $registryPath -ErrorAction Stop).GetValue("")
            if ($registered) { $candidates.Add([string]$registered) }
        } catch {}
    }
    foreach ($candidate in @(
        $(if ($env:ProgramW6432) { Join-Path $env:ProgramW6432 "Docker\Docker\Docker Desktop.exe" }),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe" }),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe")
    )) {
        if ($candidate) { $candidates.Add([string]$candidate) }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    return $null
}

function Add-DockerCliToProcessPath {
    $candidates = @(
        $(if ($env:ProgramW6432) { Join-Path $env:ProgramW6432 "Docker\Docker\resources\bin" }),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate "docker.exe") -PathType Leaf)) {
            $entries = @($env:Path -split ";")
            if ($entries -notcontains $candidate) { $env:Path = "$candidate;$env:Path" }
            return
        }
    }
}

function Install-AndResolveDockerDesktop {
    $desktopExecutable = Find-DockerDesktopExecutable
    if ($desktopExecutable) {
        Add-DockerCliToProcessPath
        return $desktopExecutable
    }

    $wingetExecutable = Find-WingetExecutable
    if (-not $wingetExecutable) {
        throw "Docker Desktop is missing and Windows Package Manager (winget) is unavailable. Install Docker Desktop, ensure it uses the WSL 2 Linux-container backend, and rerun Corvus Setup."
    }

    Write-Host "Docker Desktop is not installed. Installing it now; Windows may request administrator approval..."
    Invoke-External $wingetExecutable @(
        "install", "--id", "Docker.DockerDesktop", "--exact", "--source", "winget",
        "--silent", "--accept-source-agreements", "--accept-package-agreements",
        "--disable-interactivity"
    ) -Stream

    $deadline = [DateTime]::UtcNow.AddSeconds(180)
    do {
        Refresh-ProcessPath
        $desktopExecutable = Find-DockerDesktopExecutable
        if ($desktopExecutable) { break }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $desktopExecutable) {
        throw "Docker Desktop reported a successful installation but its executable was not found within 180 seconds. Restart Windows if requested, then rerun Corvus Setup."
    }
    Add-DockerCliToProcessPath
    Write-Host "Docker Desktop is installed."
    return $desktopExecutable
}

function Test-DockerEngineReady([int]$ProbeTimeoutSeconds = 6) {
    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) { $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue }
    if (-not $dockerCommand) { return $false }

    $process = $null
    try {
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = [string]$dockerCommand.Source
        $startInfo.Arguments = 'info --format "{{.ServerVersion}}"'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            Write-Host "Docker engine probe could not start."
            return $false
        }
        if (-not $process.WaitForExit($ProbeTimeoutSeconds * 1000)) {
            Write-Warning "Docker engine probe exceeded $ProbeTimeoutSeconds seconds; stopping the unresponsive client."
            & taskkill.exe /PID $process.Id /T /F *> $null
            return $false
        }
        $version = $process.StandardOutput.ReadToEnd().Trim()
        $detail = $process.StandardError.ReadToEnd().Trim()
        if ($process.ExitCode -ne 0) {
            if ($detail) { Write-Host "Docker engine probe is not ready: $detail" }
            return $false
        }
        return -not [string]::IsNullOrWhiteSpace($version)
    }
    catch {
        Write-Host "Docker engine probe could not complete: $($_.Exception.Message)"
        return $false
    }
    finally {
        if ($process) {
            if (-not $process.HasExited) {
                try { & taskkill.exe /PID $process.Id /T /F *> $null } catch {}
            }
            $process.Dispose()
        }
    }
}

function Show-DockerRetryDialog {
    Add-Type -AssemblyName System.Windows.Forms
    $message = @"
Docker Desktop is not ready.

Open Docker Desktop manually and wait until it shows that the engine is running.

Then click Retry. Corvus will check the engine again.

Click Cancel to stop setup.
"@
    return [System.Windows.Forms.MessageBox]::Show(
        $message,
        "Corvus Setup - Start Docker Desktop",
        [System.Windows.Forms.MessageBoxButtons]::RetryCancel,
        [System.Windows.Forms.MessageBoxIcon]::Information
    )
}

function Start-AndWaitDockerDesktop([int]$TimeoutSeconds = 60) {
    if (Test-DockerEngineReady) { return }

    $desktopExecutable = Find-DockerDesktopExecutable
    $desktopRunning = @(Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue).Count -gt 0
    if (-not $desktopRunning -and $desktopExecutable) {
        Write-Host "Docker Desktop is not running. Attempting to start it..."
        try {
            Start-Process -FilePath $desktopExecutable | Out-Null
        } catch {
            Write-Warning "Docker Desktop could not be started automatically: $($_.Exception.Message)"
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-DockerEngineReady) {
            Write-Host "Docker Desktop's Linux-container engine is ready."
            return
        }
        Start-Sleep -Seconds 5
    }

    while (-not (Test-DockerEngineReady)) {
        Write-Warning "Docker Desktop is not ready. The user must start it manually before setup can continue."
        $choice = Show-DockerRetryDialog
        if ($choice -eq [System.Windows.Forms.DialogResult]::Cancel) {
            throw "Docker Desktop is not running. Setup was cancelled."
        }
        Write-Host "Retry selected. Checking Docker Desktop's Linux-container engine again..."
    }

    Write-Host "Docker Desktop's Linux-container engine is ready. Continuing setup..."
}

function Test-PortAvailable([int]$Port) {
    $listener = New-Object Net.Sockets.TcpListener -ArgumentList ([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        try { $listener.Stop() } catch {}
    }
}

function Repair-IncompleteInstall {
    if ((Test-Path -LiteralPath $resultPath -PathType Leaf) -or -not (Test-Path -LiteralPath $appRoot)) {
        return
    }

    Write-Host "A previous Corvus installation did not finish. Cleaning the incomplete application checkout before retrying..."
    $composePath = Join-Path $appRoot "docker-compose.yml"
    if (Test-Path -LiteralPath $composePath -PathType Leaf) {
        Invoke-External "docker" @("compose", "-f", $composePath, "down", "--remove-orphans") -IgnoreExitCode | Out-Null
    }
    Remove-Item -LiteralPath $appRoot -Recurse -Force -ErrorAction Stop
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    Write-Host "Incomplete Corvus application files were removed. Persistent Docker data volumes were preserved."
}

function Find-AvailablePort([int]$Preferred, [int]$Maximum, [int[]]$Exclude = @()) {
    for ($port = $Preferred; $port -le $Maximum; $port++) {
        if ($Exclude -contains $port) { continue }
        if (Test-PortAvailable $port) { return $port }
    }
    for ($attempt = 0; $attempt -lt 16; $attempt++) {
        $listener = New-Object Net.Sockets.TcpListener -ArgumentList ([Net.IPAddress]::Loopback, 0)
        try {
            $listener.Start()
            $candidate = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
            if ($candidate -ge 1 -and $candidate -le 65535 -and -not ($Exclude -contains $candidate)) {
                return $candidate
            }
        } catch {
        } finally {
            try { $listener.Stop() } catch {}
        }
    }
    throw "Windows could not allocate a usable loopback TCP port after the preferred range $Preferred-$Maximum was unavailable."
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $content = if (Test-Path -LiteralPath $Path) {
        Get-Content -LiteralPath $Path -Raw
    } else {
        ""
    }
    $lines = New-Object Collections.Generic.List[string]
    $found = $false
    foreach ($line in ($content -split "`r?`n")) {
        if ($line.StartsWith("$Key=")) {
            $lines.Add("$Key=$Value")
            $found = $true
        } elseif ($line.Length -gt 0) {
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllText(
        $Path,
        (($lines -join [Environment]::NewLine) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
}

function Get-EnvValues([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -and -not $line.TrimStart().StartsWith("#") -and $line.Contains("=")) {
            $separator = $line.IndexOf("=")
            $values[$line.Substring(0, $separator).Trim()] = $line.Substring($separator + 1).Trim()
        }
    }
    return $values
}

try {
    try {
        Start-Transcript -Path (Join-Path $logRoot "install.log") -Append | Out-Null
        $transcriptStarted = $true
    } catch {}

    if ($env:OS -ne "Windows_NT") {
        throw "The standard installer supports Windows 10 and Windows 11 only."
    }
    if (-not (Test-Path -LiteralPath $BundlePath -PathType Leaf)) {
        throw "The embedded Corvus source bundle is missing."
    }
    if ($RepositoryUrl -notmatch '^(https?|ssh)://[^\s]+$' -and $RepositoryUrl -notmatch '^[^@\s]+@[^:\s]+:.+$') {
        throw "RepositoryUrl must be HTTP(S), SSH, or Git's user@host:path form."
    }

    Write-Host "Installer log: $(Join-Path $logRoot 'install.log')"
    Write-Host "Phase 1/5: installing and validating Git, WSL 2 and Docker Desktop prerequisites..."
    $gitExecutable = Install-AndResolveGitForWindows
    Install-AndEnsureWsl2
    $dockerDesktopExecutable = Install-AndResolveDockerDesktop
    Refresh-ProcessPath
    Assert-Command "docker" "Docker Desktop was installed but docker.exe is not available. Restart Windows if requested, then rerun Corvus Setup."
    Invoke-External "docker" @("compose", "version") -Capture | Out-Null
    Write-Host "Phase 2/5: starting Docker Desktop and requiring a working Linux-container engine..."
    Start-AndWaitDockerDesktop -TimeoutSeconds 240
    if (-not (Test-DockerEngineReady)) {
        throw "Docker Desktop is installed but its Linux-container engine is not running. Corvus will not proceed until docker info succeeds."
    }

    Repair-IncompleteInstall


    Write-Host "Phase 3/5: preparing the local application checkout from the embedded release bundle..."
    if (Test-Path -LiteralPath (Join-Path $appRoot ".git") -PathType Container) {
        # A packaged release must not require the user to manage Git. Preserve
        # unexpected edits and untracked files in a recoverable stash, rather
        # than discarding them or letting them block an automatic upgrade.
        # Ignored files (notably .env and local data) remain in place.
        $changes = Invoke-External $gitExecutable @("-C", $appRoot, "status", "--porcelain", "--untracked-files=normal") -Capture
        $savedStash = ""
        if ($changes) {
            $beforeStash = Invoke-External $gitExecutable @("-C", $appRoot, "rev-parse", "-q", "--verify", "refs/stash") -Capture -IgnoreExitCode
            Invoke-External $gitExecutable @("-C", $appRoot, "stash", "push", "--include-untracked", "-m", "Corvus installer backup $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')") -Capture | Out-Null
            $savedStash = Invoke-External $gitExecutable @("-C", $appRoot, "rev-parse", "-q", "--verify", "refs/stash") -Capture
            if (-not $savedStash -or $savedStash -eq $beforeStash) {
                throw "Corvus could not back up the existing application changes; the upgrade was stopped without deleting them."
            }
            Write-Host "Existing application modifications were safely backed up in Git stash $savedStash. Local configuration and Docker volumes are unchanged."
        }
        try {
            Update-AppCheckoutFromBundle -GitExecutable $gitExecutable -AppRoot $appRoot -BundlePath $BundlePath
        } catch {
            # If updating the source failed before moving HEAD, restore the
            # user's working tree. Keep the stash if applying it fails.
            if ($savedStash) {
                try {
                    Invoke-External $gitExecutable @("-C", $appRoot, "stash", "apply", $savedStash) -Capture | Out-Null
                    Write-Host "Previous application modifications restored after the failed upgrade."
                } catch {
                    Write-Warning "Could not automatically restore prior modifications; they remain recoverable from Git stash $savedStash."
                }
            }
            throw
        }
    } else {
        if (Test-Path -LiteralPath $appRoot) {
            $entries = @(Get-ChildItem -LiteralPath $appRoot -Force -ErrorAction SilentlyContinue)
            if ($entries.Count -gt 0) {
                throw "The installation target '$appRoot' exists and is not a Corvus Git checkout."
            }
            Remove-Item -LiteralPath $appRoot -Force
        }
        Invoke-External $gitExecutable @(
            "clone", "--branch", "main", "--single-branch", $BundlePath, $appRoot
        ) -Stream
    }
    Write-Host "The embedded application checkout is ready."

    $origin = Invoke-External $gitExecutable @("-C", $appRoot, "remote", "get-url", "origin") -Capture -IgnoreExitCode
    if ($origin) {
        Invoke-External $gitExecutable @("-C", $appRoot, "remote", "set-url", "origin", $RepositoryUrl)
    } else {
        Invoke-External $gitExecutable @("-C", $appRoot, "remote", "add", "origin", $RepositoryUrl)
    }
    Invoke-External $gitExecutable @("-C", $appRoot, "config", "branch.main.remote", "origin")
    Invoke-External $gitExecutable @("-C", $appRoot, "config", "branch.main.merge", "refs/heads/main")

    $envPath = Join-Path $appRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $appRoot ".env.example") -Destination $envPath
        $workbenchPort = Find-AvailablePort 8000 8099
        $browserPort = Find-AvailablePort 6080 6179 @($workbenchPort)
        Set-EnvValue $envPath "OSINT_PORT" ([string]$workbenchPort)
        Set-EnvValue $envPath "OSINT_BROWSER_DESKTOP_PORT" ([string]$browserPort)
        Set-EnvValue $envPath "OSINT_BROWSER_DESKTOP_URL" "http://127.0.0.1:$browserPort/vnc.html?autoconnect=true&resize=scale"
    }

    $installScript = Join-Path $appRoot "Install-OSINTWorkbench.ps1"
    $installArguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $installScript,
        "-DailyUpdateTime", $DailyUpdateTime,
        "-NoShortcuts", "-NoLaunch"
    )
    if ($NoDailyUpdate) { $installArguments += "-NoDailyUpdate" }
    Write-Host "Phase 4/5: prerequisite gate passed; Docker Engine is running."
    Write-Host "Phase 5/5: building and starting the Docker services. The first build can take 10 to 30 minutes."
    Write-Host "Docker build output and health-check progress will appear below."
    Invoke-External "powershell.exe" $installArguments -Stream
    Write-Host "Docker build and health checks completed."

    $values = Get-EnvValues $envPath
    $workbenchUrl = "http://127.0.0.1:" + $(if ($values.ContainsKey("OSINT_PORT")) { $values["OSINT_PORT"] } else { "8000" })
    $browserUrl = if ($values.ContainsKey("OSINT_BROWSER_DESKTOP_URL")) {
        $values["OSINT_BROWSER_DESKTOP_URL"]
    } else {
        "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
    }
    $commit = Invoke-External $gitExecutable @("-C", $appRoot, "rev-parse", "HEAD") -Capture
    $result = @(
        "[OSINTWorkbench]",
        "AppRoot=$appRoot",
        "WorkbenchUrl=$workbenchUrl",
        "BrowserUrl=$browserUrl",
        "Commit=$commit"
    ) -join [Environment]::NewLine
    [IO.File]::WriteAllText($resultPath, $result + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Corvus installation completed successfully."
} catch {
    $rawMessage = $_.Exception.Message
    $message = Convert-ToFriendlyInstallError $rawMessage
    [IO.File]::WriteAllText(
        (Join-Path $logRoot "install-error.txt"),
        $message + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    Write-Host "Technical detail has been retained in $(Join-Path $logRoot 'install.log')." -ForegroundColor Yellow
    Write-Error $rawMessage
    exit 1
} finally {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    if ($transcriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

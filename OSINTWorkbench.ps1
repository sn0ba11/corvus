param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "update", "rollback", "backup", "status", "shortcuts")]
    [string]$Command,
    [switch]$Browser,
    [switch]$ForceAppRebuild,
    [string]$BuildCommitOverride
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$StatePath = Join-Path $Root ".install-state.json"
$UnknownBuildCommit = "0000000000000000000000000000000000000000"
$CoreServices = @(
    "app", "maigret", "sherlock", "naminter", "holehe", "theharvester", "subfinder",
    "instaloader", "gallery-dl", "yt-dlp", "nexfil", "social_analyzer", "searxng", "browser-agent"
)
$AllServices = $CoreServices
$BuildServices = @(
    "app", "tor", "searxng", "metasearch2", "browser-agent",
    "maigret", "sherlock", "naminter", "holehe", "theharvester", "subfinder",
    "instaloader", "gallery-dl", "yt-dlp", "nexfil", "social_analyzer"
)
$ImageTags = @{
    "app" = "corvus-app:latest"
    "maigret" = "corvus-maigret:latest"
    "sherlock" = "corvus-sherlock:latest"
    "naminter" = "corvus-naminter:latest"
    "holehe" = "corvus-holehe:latest"
    "theharvester" = "corvus-theharvester:latest"
    "subfinder" = "corvus-subfinder:latest"
    "instaloader" = "corvus-instaloader:latest"
    "gallery-dl" = "corvus-gallery-dl:latest"
    "yt-dlp" = "corvus-yt-dlp:latest"
    "nexfil" = "corvus-nexfil:latest"
    "social_analyzer" = "corvus-social-analyzer:latest"
    "searxng" = "corvus-searxng:latest"
    "browser-agent" = "corvus-browser-agent:latest"
}

function Repair-UpdateProtocol {
    if ($env:OS -ne "Windows_NT") { return }
    $registrationScript = Join-Path $Root "Register-OSINTUpdateProtocol.ps1"
    try {
        & $registrationScript -Root $Root
    } catch {
        Write-Warning "The update launcher could not be registered: $($_.Exception.Message)"
    }
}

function Ensure-CorvusIcon {
    $icon = Join-Path $Root "installer\OSINTWorkbench.ico"
    if (Test-Path -LiteralPath $icon -PathType Leaf) { return $icon }
    $generator = Join-Path $Root "installer\windows\New-OSINTWorkbenchIcon.ps1"
    if (-not (Test-Path -LiteralPath $generator -PathType Leaf)) {
        throw "Corvus icon generator is missing from '$generator'."
    }
    & $generator -OutputPath $icon
    if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
        throw "Corvus icon generation did not create '$icon'."
    }
    return $icon
}

function Refresh-WindowsShortcuts {
    if ($env:OS -ne "Windows_NT") { return }
    $values = Get-EnvValues
    $port = if ($values.ContainsKey("OSINT_PORT")) { [string]$values["OSINT_PORT"] } else { "8000" }
    $workbenchUrl = "http://127.0.0.1:$port"

    $desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    if ([string]::IsNullOrWhiteSpace($desktop)) {
        throw "Windows did not return the current user's Desktop folder; Corvus cannot create the required Desktop shortcut."
    }
    $startMenuBase = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    if ([string]::IsNullOrWhiteSpace($startMenuBase)) {
        throw "Windows did not return the current user's Start Menu Programs folder; Corvus cannot create the required Start Menu shortcut."
    }
    $startMenu = Join-Path $startMenuBase "Corvus"
    New-Item -ItemType Directory -Path $desktop -Force | Out-Null
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null

    $icon = Ensure-CorvusIcon
    $shell = New-Object -ComObject WScript.Shell
    foreach ($target in @($desktop, $startMenu)) {
        Remove-Item -LiteralPath (Join-Path $target "Corvus.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $target "Corvus Sock Browser.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $target "OSINT Sock Browser.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $target "Corvus Sock Browser.lnk") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $target "OSINT Sock Browser.lnk") -Force -ErrorAction SilentlyContinue

        $shortcutPath = Join-Path $target "Corvus.lnk"
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = (Join-Path $env:WINDIR "explorer.exe")
        $shortcut.Arguments = $workbenchUrl
        $shortcut.WorkingDirectory = $Root
        $shortcut.Description = "Open Corvus"
        $shortcut.IconLocation = "$icon,0"
        $shortcut.Save()
        if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
            throw "Corvus shortcut creation reported success but '$shortcutPath' was not created."
        }
    }

    Remove-Item -LiteralPath (Join-Path $startMenu "Check for Updates.lnk") -Force -ErrorAction SilentlyContinue
    $updateShortcutPath = Join-Path $startMenu "Corvus Update.lnk"
    $updateShortcut = $shell.CreateShortcut($updateShortcutPath)
    $updateShortcut.TargetPath = (Join-Path $env:WINDIR "explorer.exe")
    $updateShortcut.Arguments = "corvus-update://run"
    $updateShortcut.WorkingDirectory = $Root
    $updateShortcut.IconLocation = "$icon,0"
    $updateShortcut.Save()
    if (-not (Test-Path -LiteralPath $updateShortcutPath -PathType Leaf)) {
        throw "Corvus update shortcut creation reported success but '$updateShortcutPath' was not created."
    }

    Write-Host "Desktop shortcut verified: $(Join-Path $desktop 'Corvus.lnk')"
    Write-Host "Start Menu shortcut verified: $(Join-Path $startMenu 'Corvus.lnk')"
}

function Get-EnvValues {
    $values = @{}
    $path = Join-Path $Root ".env"
    if (-not (Test-Path -LiteralPath $path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -and -not $line.TrimStart().StartsWith("#") -and $line.Contains("=")) {
            $separator = $line.IndexOf("=")
            $key = $line.Substring(0, $separator).Trim()
            $value = $line.Substring($separator + 1).Trim()
            $values[$key] = $value
        }
    }
    return $values
}

function Get-SourceVersion {
    # pyproject.toml is the authoritative package version. Git tags may lag HEAD,
    # and the installed .env must never be used as the new version source: that
    # preserves stale OSINT_VERSION=0.1.0 across successful Git updates.
    $projectPath = Join-Path $Root "pyproject.toml"
    if (Test-Path -LiteralPath $projectPath -PathType Leaf) {
        $inProject = $false
        foreach ($line in Get-Content -LiteralPath $projectPath) {
            if ($line -match '^\s*\[([^]]+)\]\s*$') {
                $inProject = $Matches[1] -eq "project"
                continue
            }
            if ($inProject -and $line -match '^\s*version\s*=\s*"([^"]+)"\s*$') {
                return $Matches[1]
            }
        }
    }
    throw "Corvus package version could not be read from pyproject.toml."
}

function Get-SourceCommit {
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
    if ((Test-Path -LiteralPath (Join-Path $Root ".git")) -and $gitCommand) {
        $gitExecutable = if ($gitCommand.Source) { $gitCommand.Source } elseif ($gitCommand.Path) { $gitCommand.Path } else { "git" }
        Push-Location $Root
        try {
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $rawCommit = & $gitExecutable -c "safe.directory=$Root" rev-parse HEAD 2>$null | Select-Object -First 1
                $exitCode = $LASTEXITCODE
                $commit = if ($rawCommit) { ([string]$rawCommit).Trim().ToLowerInvariant() } else { "" }
            } finally {
                $ErrorActionPreference = $previousPreference
            }
            if ($exitCode -eq 0 -and $commit -match '^[0-9a-f]{40}$') { return $commit }
        } finally {
            Pop-Location
        }
    }
    $values = Get-EnvValues
    if ($values.ContainsKey("OSINT_BUILD_COMMIT")) {
        $configured = [string]$values["OSINT_BUILD_COMMIT"]
        if ($configured -match '^[0-9a-fA-F]{7,40}$') { return $configured.ToLowerInvariant() }
    }
    return $UnknownBuildCommit
}

function Get-Services {
    return $CoreServices
}

function New-RandomToken([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Set-EnvValue([string]$Content, [string]$Key, [string]$Value) {
    $lines = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($line in ($Content -split "`r?`n")) {
        if ($line.StartsWith("$Key=")) {
            $lines.Add("$Key=$Value")
            $found = $true
        } elseif ($line.Length -gt 0) {
            $lines.Add($line)
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    return ($lines -join [Environment]::NewLine) + [Environment]::NewLine
}

function Test-LoopbackPortAvailable([int]$Port) {
    if ($Port -lt 1 -or $Port -gt 65535) { return $false }
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

function Find-AvailableLoopbackPort([int]$Preferred, [int]$Maximum, [int[]]$Exclude = @()) {
    for ($port = $Preferred; $port -le $Maximum; $port++) {
        if ($Exclude -contains $port) { continue }
        if (Test-LoopbackPortAvailable $port) { return $port }
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

function Assert-ConfiguredPorts {
    $values = Get-EnvValues
    $seen = New-Object Collections.Generic.HashSet[int]
    foreach ($key in @("OSINT_PORT", "OSINT_BROWSER_DESKTOP_PORT")) {
        $raw = if ($values.ContainsKey($key)) { [string]$values[$key] } elseif ($key -eq "OSINT_PORT") { "8000" } else { "6080" }
        $parsed = 0
        if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
            throw "$key must be a valid TCP port between 1 and 65535."
        }
        if (-not $seen.Add($parsed)) {
            throw "OSINT_PORT and OSINT_BROWSER_DESKTOP_PORT must use different ports."
        }
    }
}

function Sync-BrowserDesktopUrl {
    $path = Join-Path $Root ".env"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Cannot synchronize browser URL because .env is missing."
    }
    $content = Get-Content -LiteralPath $path -Raw
    $values = Get-EnvValues
    $browserPort = if ($values.ContainsKey("OSINT_BROWSER_DESKTOP_PORT")) {
        [string]$values["OSINT_BROWSER_DESKTOP_PORT"]
    } else {
        "6080"
    }
    $content = Set-EnvValue $content "OSINT_BROWSER_DESKTOP_URL" "http://127.0.0.1:$browserPort/vnc.html?autoconnect=true&resize=scale"
    [IO.File]::WriteAllText($path, $content, (New-Object Text.UTF8Encoding($false)))
}

function Ensure-AvailablePorts {
    $path = Join-Path $Root ".env"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Cannot validate host ports because .env is missing."
    }
    $content = Get-Content -LiteralPath $path -Raw
    $values = Get-EnvValues
    $assignments = @(
        @{ Key = "OSINT_PORT"; Preferred = 8000; Maximum = 8099 },
        @{ Key = "OSINT_BROWSER_DESKTOP_PORT"; Preferred = 6080; Maximum = 6179 }
    )
    $reservedSelections = New-Object Collections.Generic.List[int]
    foreach ($assignment in $assignments) {
        $key = [string]$assignment.Key
        $preferred = [int]$assignment.Preferred
        $maximum = [int]$assignment.Maximum
        $selected = $preferred
        if ($values.ContainsKey($key)) {
            $parsed = 0
            if ([int]::TryParse([string]$values[$key], [ref]$parsed) -and $parsed -ge 1 -and $parsed -le 65535) {
                $selected = $parsed
            }
        }
        if (-not (Test-LoopbackPortAvailable $selected) -or $reservedSelections.Contains($selected)) {
            $originalSelected = $selected
            $replacement = Find-AvailableLoopbackPort $preferred $maximum $reservedSelections.ToArray()
            $content = Set-EnvValue $content $key ([string]$replacement)
            $values[$key] = [string]$replacement
            $selected = $replacement
            Write-Host "Loopback port $originalSelected is occupied or unavailable; $key was changed to $replacement."
        }
        $reservedSelections.Add($selected)
    }
    $browserPort = if ($values.ContainsKey("OSINT_BROWSER_DESKTOP_PORT")) {
        [string]$values["OSINT_BROWSER_DESKTOP_PORT"]
    } else {
        "6080"
    }
    $content = Set-EnvValue $content "OSINT_BROWSER_DESKTOP_URL" "http://127.0.0.1:$browserPort/vnc.html?autoconnect=true&resize=scale"
    [IO.File]::WriteAllText($path, $content, (New-Object Text.UTF8Encoding($false)))
}

function Ensure-UpdateChangelog {
    $installedPath = Join-Path $Root ".last-update-changes.txt"
    $installedRangePath = Join-Path $Root ".last-update-range.json"
    $runtimeRoot = Join-Path $Root "runtime"
    $runtimePath = Join-Path $runtimeRoot "last-update-changes.txt"
    $runtimeRangePath = Join-Path $runtimeRoot "last-update-range.json"
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    if (-not (Test-Path -LiteralPath $installedPath -PathType Leaf)) {
        $content = "Corvus changes installed:`r`nInitial installation.`r`n"
        [IO.File]::WriteAllText(
            $installedPath,
            $content,
            (New-Object Text.UTF8Encoding($false))
        )
    }
    if (-not (Test-Path -LiteralPath $installedRangePath -PathType Leaf)) {
        $current = Get-SourceCommit
        $existingInstall = Test-Path -LiteralPath $StatePath -PathType Leaf
        [ordered]@{
            from_commit = $(if ($existingInstall) { $UnknownBuildCommit } else { $current })
            to_commit = $current
            fallback_used = [bool]$existingInstall
        } | ConvertTo-Json | Set-Content -LiteralPath $installedRangePath -Encoding UTF8
    }
    Copy-Item -LiteralPath $installedPath -Destination $runtimePath -Force
    Copy-Item -LiteralPath $installedRangePath -Destination $runtimeRangePath -Force
}

function Ensure-Env([switch]$EnableBrowser) {
    $path = Join-Path $Root ".env"
    if (Test-Path -LiteralPath $path) {
        $content = Get-Content -LiteralPath $path -Raw
    } else {
        $content = Get-Content -LiteralPath (Join-Path $Root ".env.example") -Raw
    }
    # Browser support is integral. Accept -Browser for compatibility with older commands.
    $null = $EnableBrowser
    $content = Set-EnvValue $content "OSINT_BROWSER_ENABLED" "true"
    $content = Set-EnvValue $content "OSINT_VERSION" (Get-SourceVersion)
    $buildCommit = if ($BuildCommitOverride -match '^[0-9a-fA-F]{40}$') { $BuildCommitOverride.ToLowerInvariant() } else { Get-SourceCommit }
    $content = Set-EnvValue $content "OSINT_BUILD_COMMIT" $buildCommit
    $searxngSecret = ""
    $agentToken = ""
    $browserAllowedHosts = ""
    $sessionSecret = ""
    foreach ($line in ($content -split "`r?`n")) {
        if ($line.StartsWith("OSINT_SECRET_KEY=")) { $sessionSecret = $line.Substring("OSINT_SECRET_KEY=".Length).Trim() }
        if ($line.StartsWith("OSINT_SEARXNG_SECRET=")) { $searxngSecret = $line.Substring("OSINT_SEARXNG_SECRET=".Length).Trim() }
        if ($line.StartsWith("OSINT_BROWSER_AGENT_TOKEN=")) { $agentToken = $line.Substring("OSINT_BROWSER_AGENT_TOKEN=".Length).Trim() }
        if ($line.StartsWith("OSINT_BROWSER_ALLOWED_HOSTS=")) { $browserAllowedHosts = $line.Substring("OSINT_BROWSER_ALLOWED_HOSTS=".Length).Trim() }
    }
    if ($sessionSecret.Length -lt 32 -or $sessionSecret -eq "replace-with-at-least-32-random-characters") {
        $content = Set-EnvValue $content "OSINT_SECRET_KEY" (New-RandomToken 48)
    }
    if ($searxngSecret.Length -lt 32 -or $searxngSecret -eq "replace-with-a-distinct-searxng-secret") {
        $content = Set-EnvValue $content "OSINT_SEARXNG_SECRET" (New-RandomToken 48)
    }
    if ($agentToken.Length -lt 32 -or $agentToken -eq "replace-with-browser-agent-token") {
        $content = Set-EnvValue $content "OSINT_BROWSER_AGENT_TOKEN" (New-RandomToken 48)
    }
    $exampleHostsLine = Get-Content -LiteralPath (Join-Path $Root ".env.example") | Where-Object { $_.StartsWith("OSINT_BROWSER_ALLOWED_HOSTS=") } | Select-Object -First 1
    $exampleHosts = if ($exampleHostsLine) { $exampleHostsLine.Substring("OSINT_BROWSER_ALLOWED_HOSTS=".Length).Trim() } else { "" }
    if (-not $browserAllowedHosts) { $browserAllowedHosts = $exampleHosts }
    $hostValues = @($browserAllowedHosts.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
    foreach ($requiredHost in @($exampleHosts.Split(",") | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })) {
        if ($hostValues -notcontains $requiredHost) { $hostValues += $requiredHost }
    }
    $content = Set-EnvValue $content "OSINT_BROWSER_ALLOWED_HOSTS" (($hostValues | Select-Object -Unique) -join ",")
    [IO.File]::WriteAllText($path, $content, (New-Object Text.UTF8Encoding($false)))
}

function Invoke-Docker([string[]]$Arguments, [switch]$Capture, [switch]$IgnoreExitCode) {
    Push-Location $Root
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Capture) {
            $output = & docker @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        } else {
            $output = & docker @Arguments 2>&1
            $exitCode = $LASTEXITCODE
            if ($output) { $output | ForEach-Object { Write-Host ([string]$_) } }
        }
    } finally {
        $ErrorActionPreference = $previousPreference
        Pop-Location
    }
    if ($exitCode -ne 0) {
        if (-not $IgnoreExitCode) {
            $details = if ($output) { "`n" + ($output -join [Environment]::NewLine) } else { "" }
            throw "docker $($Arguments -join ' ') failed with exit code $exitCode.$details"
        }
        if ($Capture) { return "" }
        return
    }
    if ($Capture) { return ($output -join [Environment]::NewLine).Trim() }
}

function Invoke-Compose([string[]]$Arguments, [switch]$Capture, [switch]$IgnoreExitCode) {
    $dockerArguments = @("compose")
    $dockerArguments += $Arguments
    return (Invoke-Docker -Arguments $dockerArguments -Capture:$Capture -IgnoreExitCode:$IgnoreExitCode)
}

function Invoke-ComposeWithRetry(
    [string[]]$Arguments,
    [int]$Attempts = 3,
    [int]$DelaySeconds = 8
) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Invoke-Compose -Arguments $Arguments
            return
        } catch {
            if ($attempt -ge $Attempts) { throw }
            Write-Warning "Docker Compose failed on attempt $attempt of $Attempts. Waiting for the Docker engine before retrying."
            if ($env:OS -eq "Windows_NT") {
                Start-AndWaitDockerDesktop -TimeoutSeconds 120
            } elseif (-not (Test-DockerEngineReady)) {
                throw
            }
            Start-Sleep -Seconds $DelaySeconds
        }
    }
}

function Find-DockerDesktopExecutable {
    if ($env:OS -ne "Windows_NT") { return $null }
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

function Test-DockerEngineReady {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & docker info --format "{{.ServerVersion}}" 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace(($output -join ""))
}

function Start-AndWaitDockerDesktop([int]$TimeoutSeconds = 240) {
    if (Test-DockerEngineReady) { return }
    if ($env:OS -ne "Windows_NT") {
        throw "The Docker engine is not running. Start Docker and retry."
    }

    $desktopExecutable = Find-DockerDesktopExecutable
    $desktopRunning = @(Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue).Count -gt 0
    if (-not $desktopRunning -and $desktopExecutable) {
        Write-Host "Docker Desktop is not running. Starting it now..."
        try {
            Start-Process -FilePath $desktopExecutable | Out-Null
        } catch {
            Write-Warning "Docker Desktop could not be started automatically: $($_.Exception.Message)"
        }
    } elseif ($desktopRunning) {
        Write-Host "Docker Desktop is running; waiting for its Linux-container engine..."
    } else {
        Write-Warning "Docker Desktop's executable was not found in a standard location. Start Docker Desktop manually; setup will continue waiting."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $nextProgress = [DateTime]::UtcNow
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-DockerEngineReady) {
            Write-Host "Docker Desktop's Linux-container engine is ready."
            return
        }
        if ([DateTime]::UtcNow -ge $nextProgress) {
            $remaining = [Math]::Max(0, [int]($deadline - [DateTime]::UtcNow).TotalSeconds)
            Write-Host "Waiting for Docker Desktop... up to $remaining seconds remaining."
            $nextProgress = [DateTime]::UtcNow.AddSeconds(15)
        }
        Start-Sleep -Seconds 3
    }
    throw "Docker Desktop did not make its Linux-container engine available within $TimeoutSeconds seconds. Start Docker Desktop, wait until it reports Engine running, and retry."
}

function Assert-Prerequisites {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker Desktop with Compose v2 is required."
    }
    Invoke-Docker @("compose", "version") -Capture | Out-Null
    Start-AndWaitDockerDesktop
}

function Convert-ComposeJson([string]$Text) {
    if (-not $Text) { return @() }
    $trimmed = $Text.Trim()
    if (-not $trimmed) { return @() }
    if ($trimmed.StartsWith("[")) {
        try { return @($trimmed | ConvertFrom-Json -ErrorAction Stop) } catch { return @() }
    }
    $items = @()
    foreach ($line in ($trimmed -split "`r?`n")) {
        if ($line.Trim()) {
            try { $items += $line | ConvertFrom-Json -ErrorAction Stop } catch {}
        }
    }
    return $items
}

function Wait-Healthy([int]$DeadlineSeconds = 180) {
    $deadline = [DateTime]::UtcNow.AddSeconds($DeadlineSeconds)
    $expected = @(Get-Services)
    while ([DateTime]::UtcNow -lt $deadline) {
        $output = Invoke-Compose @("ps", "--format", "json") -Capture -IgnoreExitCode
        $states = @{}
        foreach ($item in (Convert-ComposeJson $output)) {
            $states[[string]$item.Service] = @([string]$item.State, [string]$item.Health)
        }
        $healthy = $true
        foreach ($service in $expected) {
            if (-not $states.ContainsKey($service) -or $states[$service][0] -ne "running" -or $states[$service][1] -ne "healthy") {
                $healthy = $false
                break
            }
        }
        if ($healthy) { return $true }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Write-ComposeDiagnostics([string]$Context) {
    Write-Warning "$Context failed. Capturing Corvus container status and unhealthy-service logs."
    Invoke-Compose @("ps") -IgnoreExitCode
    $output = Invoke-Compose @("ps", "--format", "json") -Capture -IgnoreExitCode
    $problemServices = New-Object Collections.Generic.List[string]
    foreach ($item in (Convert-ComposeJson $output)) {
        $service = [string]$item.Service
        $state = [string]$item.State
        $health = [string]$item.Health
        if ($service -and ($state -ne "running" -or ($health -and $health -ne "healthy"))) {
            $problemServices.Add($service)
        }
    }
    foreach ($service in @($problemServices | Select-Object -Unique)) {
        Write-Host ""
        Write-Host "--- $service logs (last 120 lines) ---" -ForegroundColor Yellow
        Invoke-Compose @("logs", "--no-color", "--tail", "120", $service) -IgnoreExitCode
    }
}

function Get-ImageSnapshot {
    $images = [ordered]@{}
    foreach ($service in (Get-Services)) {
        $container = Invoke-Compose @("ps", "-q", $service) -Capture -IgnoreExitCode
        if ($container) {
            $image = Invoke-Docker @("inspect", "--format", "{{.Image}}", $container) -Capture -IgnoreExitCode
            if ($image) { $images[$service] = $image }
        }
    }
    return $images
}

function Test-ServiceRunning([string]$Service) {
    $container = Invoke-Compose @("ps", "-q", $Service) -Capture -IgnoreExitCode
    if (-not $container) { return $false }
    $running = Invoke-Docker @("inspect", "--format", "{{.State.Running}}", $container) -Capture -IgnoreExitCode
    return $running.Trim().ToLowerInvariant() -eq "true"
}

function Backup-Database {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $code = "import sqlite3,pathlib; p=pathlib.Path('/data/backups'); p.mkdir(exist_ok=True); s=sqlite3.connect('/data/osint.db'); d=sqlite3.connect(str(p/'osint-$stamp.db')); s.backup(d); d.close(); s.close()"
    if (Test-ServiceRunning "app") {
        Invoke-Compose @("exec", "-T", "app", "python", "-c", $code)
    } else {
        Write-Host "Application container is stopped; creating the backup through a temporary recovery container."
        Invoke-Compose @("run", "--rm", "--no-deps", "--entrypoint", "python", "app", "-c", $code)
    }
    Write-Host "Database backup created in the persistent volume: backups/osint-$stamp.db"
}

function Save-State($State) {
    $temporary = "$StatePath.tmp"
    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $temporary -Encoding UTF8
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        [IO.File]::Replace($temporary, $StatePath, [NullString]::Value)
    } else {
        [IO.File]::Move($temporary, $StatePath)
    }
}

function Get-RunningBuildCommit {
    try {
        $actual = Invoke-Compose @(
            "exec", "-T", "app", "python", "-c",
            "from app.core.config import get_settings; print(get_settings().build_commit.strip())"
        ) -Capture
        return ([string]$actual).Trim().ToLowerInvariant()
    } catch {
        return ""
    }
}

function Assert-RunningBuildMatchesSource {
    $expected = Get-SourceCommit
    $actual = Get-RunningBuildCommit
    if (-not $actual) {
        throw "Updated Corvus app is healthy but did not report an OSINT_BUILD_COMMIT."
    }
    if ($actual -ne $expected) {
        throw "Updated Corvus app is running build $actual but source HEAD is $expected."
    }
    Write-Host "Running Corvus build verified: $($actual.Substring(0, 8))"
}

function Ensure-ExternalVolumes {
    $volumes = @(
        "osint-workbench_workbench-data",
        "osint-workbench_browser-profile",
        "osint-workbench_browser-evidence"
    )
    foreach ($volume in $volumes) {
        $existing = Invoke-Docker -Arguments @("volume", "inspect", $volume) -Capture -IgnoreExitCode
        if (-not $existing) {
            Write-Host "Creating persistent Docker volume '$volume'..."
            Invoke-Docker -Arguments @("volume", "create", $volume) -Capture | Out-Null
        }
    }
}

function Install-Workbench {
    Assert-Prerequisites
    Ensure-ExternalVolumes
    Ensure-UpdateChangelog
    Ensure-Env -EnableBrowser:$Browser
    Ensure-AvailablePorts
    Invoke-ComposeWithRetry -Arguments @("build")
    try {
        Invoke-ComposeWithRetry -Arguments @("up", "-d")
    } catch {
        Write-ComposeDiagnostics "Docker service startup"
        throw
    }
    if (-not (Wait-Healthy)) {
        Write-ComposeDiagnostics "Installation health check"
        throw "Installation started but health checks did not pass; diagnostics are included above."
    }
    Save-State ([ordered]@{ installed_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); version = "0.2.0"; build_commit = (Get-SourceCommit); images = (Get-ImageSnapshot) })
    Refresh-WindowsShortcuts
    $values = Get-EnvValues
    $port = if ($values.ContainsKey("OSINT_PORT")) { $values["OSINT_PORT"] } else { "8000" }
    Write-Host "Corvus is ready at http://127.0.0.1:$port"
}

function Get-ChangedBuildServices {
    $allBuildServices = @($BuildServices)
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $allBuildServices }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $allBuildServices }
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        $fromCommit = [string]$state.build_commit
        $toCommit = Get-SourceCommit
        if ($fromCommit -notmatch '^[0-9a-fA-F]{40}$' -or $toCommit -notmatch '^[0-9a-fA-F]{40}$') {
            return $allBuildServices
        }
        if ($fromCommit -eq $toCommit) { return @() }
        Push-Location $Root
        try {
            $changed = @(& git -c "safe.directory=$Root" diff --name-only "$fromCommit..$toCommit" 2>$null)
            if ($LASTEXITCODE -ne 0) { return $allBuildServices }
        } finally {
            Pop-Location
        }
    } catch {
        return $allBuildServices
    }

    $selected = New-Object Collections.Generic.HashSet[string]
    $toolServices = @("maigret", "sherlock", "naminter", "holehe", "theharvester", "subfinder", "instaloader", "gallery-dl", "yt-dlp", "nexfil", "social_analyzer")
    foreach ($rawPath in $changed) {
        $path = ([string]$rawPath).Replace("\\", "/")
        if (-not $path) { continue }
        if ($path -eq "Dockerfile" -or $path -eq "pyproject.toml" -or $path -eq "LICENSE" -or $path -eq "COPYRIGHT" -or $path -eq "THIRD_PARTY_NOTICES.md" -or $path -eq "CHANGELOG.md" -or $path -eq "docs/MANUAL.md" -or $path -eq "upstreams.lock.json" -or $path -eq "alembic.ini" -or $path.StartsWith("app/") -or $path.StartsWith("migrations/") -or $path -in @("scripts/start.py", "scripts/materialise_corvus_brand.py", "scripts/download_face_models.py", "scripts/audit_username_collectors.py", "scripts/render_username_audit.py")) {
            $null = $selected.Add("app")
        }
        if ($path.StartsWith("searxng/")) { $null = $selected.Add("searxng") }
        if ($path.StartsWith("browser_agent/")) { $null = $selected.Add("browser-agent") }
        if ($path.StartsWith("torproxy/")) { $null = $selected.Add("tor") }
        if ($path.StartsWith("metasearch2/")) { $null = $selected.Add("metasearch2") }
        if ($path.StartsWith("tool_containers/") -or $path -eq "docker-compose.yml") {
            foreach ($service in $toolServices) { $null = $selected.Add($service) }
        }
    }
    return @($allBuildServices | Where-Object { $selected.Contains($_) })
}

function Update-Workbench {
    Assert-Prerequisites
    $previous = Get-ImageSnapshot
    Ensure-UpdateChangelog
    Ensure-Env -EnableBrowser:$Browser
    # An in-place update normally runs while Corvus itself owns the configured
    # host ports. Treating those listeners as conflicts caused every update to
    # rewrite .env to new ports. Preserve established bindings; only fresh
    # installs perform availability-based allocation.
    Assert-ConfiguredPorts
    Sync-BrowserDesktopUrl
    if ($previous.Count -gt 0) { Backup-Database }
    $buildServices = @(Get-ChangedBuildServices)
    $sourceCommit = Get-SourceCommit
    $runningCommit = Get-RunningBuildCommit
    $staleRunningApp = [bool]($runningCommit -and $sourceCommit -and $runningCommit -ne $sourceCommit)
    if (($ForceAppRebuild -or $staleRunningApp) -and $buildServices -notcontains "app") {
        if ($staleRunningApp) {
            Write-Host "Running app is stale ($($runningCommit.Substring(0, 8))); source is $($sourceCommit.Substring(0, 8)). Forcing app rebuild."
        }
        $buildServices = @("app") + $buildServices
    }
    # Retain the last installed build and rollback images before Docker changes tags.
    $recoveryState = [ordered]@{}
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        $installed = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        foreach ($property in $installed.PSObject.Properties) {
            $recoveryState[$property.Name] = $property.Value
        }
    }
    $recoveryState["update_started_at"] = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $recoveryState["rollback_images"] = $previous
    Save-State $recoveryState
    try {
        if ($buildServices.Count -gt 0) {
            Write-Host "Rebuilding changed Corvus images: $($buildServices -join ', ')"
            Invoke-ComposeWithRetry -Arguments (@("build", "--pull") + $buildServices)
        } else {
            Write-Host "No container image inputs changed; skipping image rebuild."
        }
        # Compose recreates only services whose image/configuration changed.
        Invoke-ComposeWithRetry -Arguments @("up", "-d", "--remove-orphans")
        if (-not (Wait-Healthy)) { throw "Update health check failed." }
        Assert-RunningBuildMatchesSource
    } catch {
        $recoveryState["failed_update_at"] = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Save-State $recoveryState
        Write-ComposeDiagnostics "Corvus update failed; recovery images retained in .install-state.json"
        throw
    }
    Save-State ([ordered]@{ updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); version = "0.2.0"; build_commit = (Get-SourceCommit); previous_images = $previous; images = (Get-ImageSnapshot) })
    Refresh-WindowsShortcuts
    Write-Host "Update completed and health checks passed."
}

function Rollback-Workbench {
    Assert-Prerequisites
    if (-not (Test-Path -LiteralPath $StatePath)) { throw "No installer state is available for rollback." }
    $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $previous = if ($state.rollback_images) { $state.rollback_images } else { $state.previous_images }
    if (-not $previous) { throw "No previous image set is recorded. Restore a previous release tree instead." }
    Ensure-UpdateChangelog
    Ensure-Env
    foreach ($property in $previous.PSObject.Properties) {
        $service = $property.Name
        if (-not $AllServices.Contains($service)) { throw "Installer state contains an invalid rollback image record." }
        Invoke-Docker @("image", "tag", [string]$property.Value, $ImageTags[$service])
    }
    Invoke-Compose @("up", "-d", "--no-build", "--remove-orphans")
    if (-not (Wait-Healthy)) { throw "Rollback images started but did not become healthy; inspect docker compose logs." }
    Write-Host "Previous application images restored. Restore the matching database backup if migrations changed."
}

Repair-UpdateProtocol

switch ($Command) {
    "install" { Install-Workbench }
    "update" { Update-Workbench }
    "rollback" { Rollback-Workbench }
    "backup" { Assert-Prerequisites; Backup-Database }
    "shortcuts" { Refresh-WindowsShortcuts }
    "status" {
        Assert-Prerequisites
        Invoke-Compose @("ps")
        if (Test-Path -LiteralPath $StatePath) { Get-Content -LiteralPath $StatePath }
    }
}

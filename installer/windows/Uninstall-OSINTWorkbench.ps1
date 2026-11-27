param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot,
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$appRoot = Join-Path $InstallRoot "app"
$logRoot = Join-Path $InstallRoot "logs"
$logPath = Join-Path $logRoot "uninstall.log"
$errorPath = Join-Path $logRoot "uninstall-error.txt"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Write-UninstallLog([string]$Message) {
    $line = "[{0}] {1}" -f ([DateTime]::UtcNow.ToString("o")), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $Message
}

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$IgnoreExitCode
    )
    Write-UninstallLog ("docker " + ($Arguments -join " "))
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & docker @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($output) {
        $output | ForEach-Object { Add-Content -LiteralPath $logPath -Value ([string]$_) -Encoding UTF8 }
    }
    if ($exitCode -ne 0 -and -not $IgnoreExitCode) {
        throw "docker $($Arguments -join ' ') failed with exit code $exitCode."
    }
    return @($output)
}

function Remove-LabelledResources([string]$Resource, [string[]]$ListArguments, [string[]]$RemovePrefix) {
    $items = @(Invoke-Docker $ListArguments -IgnoreExitCode | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    foreach ($item in $items) {
        Invoke-Docker ($RemovePrefix + @($item)) -IgnoreExitCode | Out-Null
    }
}

try {
    Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue
    try {
        Unregister-ScheduledTask -TaskName "Corvus Daily Update" -Confirm:$false -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName "OSINT Workbench Daily Update" -Confirm:$false -ErrorAction SilentlyContinue
        Write-UninstallLog "Removed the Corvus scheduled update task (and any legacy OSINT Workbench task)."
    } catch {
        Write-UninstallLog "Scheduled-task removal warning: $($_.Exception.Message)"
    }

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required to stop and remove Corvus resources. Install or restore Docker Desktop, start it, and run the uninstaller again."
    }
    try {
        Invoke-Docker @("info", "--format", "{{.ServerVersion}}") | Out-Null
    } catch {
        throw "Docker Desktop is installed but its Linux-container engine is not running. Start Docker Desktop and run the uninstaller again."
    }

    $composePath = Join-Path $appRoot "docker-compose.yml"
    $serviceImages = New-Object Collections.Generic.List[string]
    if (Test-Path -LiteralPath $composePath -PathType Leaf) {
        Push-Location $appRoot
        try {
            if ($PurgeData) {
                foreach ($image in @(Invoke-Docker @("compose", "config", "--images") -IgnoreExitCode)) {
                    $name = ([string]$image).Trim()
                    if ($name -match '^[A-Za-z0-9][A-Za-z0-9._/:@-]+$' -and -not $serviceImages.Contains($name)) {
                        $serviceImages.Add($name)
                    }
                }
                Invoke-Docker @("compose", "down", "--remove-orphans", "--volumes", "--rmi", "all") | Out-Null
            } else {
                Invoke-Docker @("compose", "down", "--remove-orphans") | Out-Null
            }
        } finally {
            Pop-Location
        }
    } elseif ($PurgeData) {
        Write-UninstallLog "Compose file was not found; removing resources by the fixed Compose project label and known names."
    }

    if ($PurgeData) {
        foreach ($projectName in @("corvus", "osint-workbench")) {
            $projectFilter = "label=com.docker.compose.project=$projectName"
            Remove-LabelledResources "containers" @("ps", "-aq", "--filter", $projectFilter) @("rm", "-f")
            Remove-LabelledResources "volumes" @("volume", "ls", "-q", "--filter", $projectFilter) @("volume", "rm", "-f")
            Remove-LabelledResources "networks" @("network", "ls", "-q", "--filter", $projectFilter) @("network", "rm")
        }

        foreach ($volume in @(
            "osint-workbench_workbench-data",
            "osint-workbench_browser-profile",
            "osint-workbench_browser-evidence"
        )) {
            Invoke-Docker @("volume", "rm", "-f", $volume) -IgnoreExitCode | Out-Null
        }
        foreach ($network in @(
            "osint-workbench_frontend",
            "osint-workbench_backend",
            "osint-workbench_egress",
            "osint-workbench_browser-control"
        )) {
            Invoke-Docker @("network", "rm", $network) -IgnoreExitCode | Out-Null
        }
        foreach ($image in @(
            $serviceImages
            "corvus-app:latest"
            "corvus-browser-agent:latest"
            "corvus-maigret:latest"
            "corvus-sherlock:latest"
            "corvus-blackbird:latest"
            "corvus-holehe:latest"
            "corvus-theharvester:latest"
            "corvus-subfinder:latest"
            "corvus-instaloader:latest"
            "corvus-gallery-dl:latest"
            "corvus-yt-dlp:latest"
            "corvus-nexfil:latest"
            "corvus-social-analyzer:latest"
            "corvus-searxng:latest"
            "osint-workbench-app:latest"
            "osint-workbench-browser-agent:latest"
            "osint-workbench-maigret:latest"
            "osint-workbench-sherlock:latest"
            "osint-workbench-blackbird:latest"
            "osint-workbench-holehe:latest"
            "osint-workbench-theharvester:latest"
            "osint-workbench-subfinder:latest"
            "osint-workbench-instaloader:latest"
            "osint-workbench-gallery-dl:latest"
            "osint-workbench-yt-dlp:latest"
            "osint-workbench-nexfil:latest"
            "osint-workbench-social-analyzer:latest"
            "osint-workbench-searxng:latest"
        ) | Select-Object -Unique) {
            Invoke-Docker @("image", "rm", "-f", ([string]$image)) -IgnoreExitCode | Out-Null
        }
        Write-UninstallLog "Full factory reset completed: Corvus containers, networks, service images and all persisted Docker volumes were removed."
    } else {
        Write-UninstallLog "Program containers were stopped. Docker volumes were preserved by examiner choice."
    }
} catch {
    $message = $_.Exception.Message
    [IO.File]::WriteAllText(
        $errorPath,
        $message + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false))
    )
    Write-Error $message
    exit 1
}

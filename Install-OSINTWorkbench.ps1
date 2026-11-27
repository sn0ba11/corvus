param(
    [switch]$Browser,
    [string]$DailyUpdateTime = "04:00",
    [switch]$NoDailyUpdate,
    [switch]$NoShortcuts,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$scriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($scriptPath)) {
    throw "Save Install-OSINTWorkbench.ps1 to disk and run the saved file; do not paste it into a PowerShell prompt."
}
$root = Split-Path -Parent $scriptPath
$workbenchScript = Join-Path $root "OSINTWorkbench.ps1"
$dailyUpdateScript = Join-Path $root "Register-OSINTDailyUpdate.ps1"
if (-not (Test-Path -LiteralPath $workbenchScript -PathType Leaf)) {
    throw "OSINTWorkbench.ps1 is missing from '$root'. Extract or clone the complete release before running the installer."
}
$dailyUpdateAvailable = $false
if (Test-Path -LiteralPath $dailyUpdateScript -PathType Leaf) {
    try {
        . $dailyUpdateScript
        $dailyUpdateAvailable = $null -ne (Get-Command Register-OSINTDailyUpdate -ErrorAction SilentlyContinue)
    } catch {
        Write-Warning "Daily-update support could not be loaded: $($_.Exception.Message)"
    }
} elseif (-not $NoDailyUpdate) {
    Write-Warning "Register-OSINTDailyUpdate.ps1 is missing. Installation will continue without the daily update task."
}

& $workbenchScript -Command install -Browser:$Browser
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$envPath = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Installation completed without creating .env. Review the OSINTWorkbench.ps1 output before retrying."
}
$envValues = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -and -not $line.TrimStart().StartsWith("#") -and $line.Contains("=")) {
        $separator = $line.IndexOf("=")
        $key = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        $envValues[$key] = $value
    }
}
$port = if ($envValues.ContainsKey("OSINT_PORT")) { $envValues["OSINT_PORT"] } else { "8000" }
$url = "http://127.0.0.1:$port"
$browserUrl = if ($envValues.ContainsKey("OSINT_BROWSER_DESKTOP_URL")) { $envValues["OSINT_BROWSER_DESKTOP_URL"] } else { "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale" }
if (-not $NoShortcuts) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        if (-not $desktop) { throw "Windows did not return a Desktop folder path." }
        $shell = New-Object -ComObject WScript.Shell
        Remove-Item -LiteralPath (Join-Path $desktop "Corvus.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $desktop "Corvus Sock Browser.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $desktop "OSINT Sock Browser.url") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $desktop "Corvus Sock Browser.lnk") -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $desktop "OSINT Sock Browser.lnk") -Force -ErrorAction SilentlyContinue
        $shortcut = $shell.CreateShortcut((Join-Path $desktop "Corvus.lnk"))
        $shortcut.TargetPath = (Join-Path $env:WINDIR "explorer.exe")
        $shortcut.Arguments = $url
        $shortcut.WorkingDirectory = $root
        $shortcut.Description = "Open Corvus"
        $icon = Join-Path $root "installer\OSINTWorkbench.ico"
        if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
            $iconGenerator = Join-Path $root "installer\windows\New-OSINTWorkbenchIcon.ps1"
            if (-not (Test-Path -LiteralPath $iconGenerator -PathType Leaf)) { throw "Corvus icon generator is missing." }
            & $iconGenerator -OutputPath $icon
        }
        if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "Corvus icon could not be generated." }
        $shortcut.IconLocation = "$icon,0"
        $shortcut.Save()
    } catch {
        Write-Warning "The application was installed, but desktop shortcuts could not be created: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Corvus Sock Browser" -ForegroundColor Cyan
Write-Host "URL: $browserUrl"
Write-Host ""

if (-not $NoDailyUpdate) {
    if ($dailyUpdateAvailable) {
        try {
            Register-OSINTDailyUpdate `
                -Root $root `
                -DailyUpdateTime $DailyUpdateTime
        } catch {
            Write-Warning "Corvus is installed, but the daily update task could not be registered: $($_.Exception.Message)"
        }
    } else {
        Write-Warning "Corvus is installed without a daily update task. Rerun the complete release installer later to add it."
    }
}
if (-not $NoLaunch) {
    try { Start-Process $url } catch { Write-Warning "Open $url manually in a browser." }
}

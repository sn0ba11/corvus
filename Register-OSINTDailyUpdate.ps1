function Register-OSINTDailyUpdate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [string]$DailyUpdateTime = "04:00",
        [string]$RepositoryUrl
    )

    [DateTime]$parsedTime = [DateTime]::MinValue
    $validTime = [DateTime]::TryParseExact(
        $DailyUpdateTime,
        "HH:mm",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$parsedTime
    )
    if (-not $validTime) { throw "DailyUpdateTime must use 24-hour HH:mm format." }
    if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
        throw "Windows Scheduled Tasks support is required for automatic updates."
    }

    $updateScript = Join-Path $Root "Update-Corvus.ps1"
    if (-not (Test-Path -LiteralPath $updateScript -PathType Leaf)) {
        throw "Update-Corvus.ps1 is missing from '$Root'."
    }
    $arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$updateScript`" -NonInteractive -OnlyIfChanged -ScheduledRun"
    if ($RepositoryUrl) {
        $arguments += " -RepositoryUrl `"$RepositoryUrl`""
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $Root
    $runAt = [DateTime]::Today.AddHours($parsedTime.Hour).AddMinutes($parsedTime.Minute)
    $trigger = New-ScheduledTaskTrigger -Daily -At $runAt
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 15) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited
    Register-ScheduledTask `
        -TaskName "Corvus Daily Update" `
        -Description "Promptless daily fast-forward update with backup, rebuild, health checks and rollback state." `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
    Write-Host "Daily updates scheduled for $DailyUpdateTime (or the next available login)."
}

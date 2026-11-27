param(
    [string]$Root = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    return
}

$updater = Join-Path $Root "Update-Corvus.ps1"
$guiUpdater = Join-Path $Root "Update-CorvusGUI.ps1"
$installRoot = Split-Path -Parent $Root
$bootstrapUpdater = Join-Path $installRoot "installer\Update-CorvusBootstrap.ps1"
if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
    throw "Corvus Update was not found at $updater"
}

$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
if (Test-Path -LiteralPath $bootstrapUpdater -PathType Leaf) {
    $currentBootstrap = Join-Path $Root "installer\windows\Update-CorvusBootstrap.ps1"
    if (Test-Path -LiteralPath $currentBootstrap -PathType Leaf) {
        Copy-Item -LiteralPath $currentBootstrap -Destination $bootstrapUpdater -Force
    }
    $command = '"{0}" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}" -InstallRoot "{2}"' -f $powershell, $bootstrapUpdater, $installRoot
} elseif (Test-Path -LiteralPath $guiUpdater -PathType Leaf) {
    $command = '"{0}" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}"' -f $powershell, $guiUpdater
} else {
    $command = '"{0}" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "{1}" -OnlyIfChanged' -f $powershell, $updater
}

$icon = Join-Path $Root "installer\OSINTWorkbench.ico"
foreach ($scheme in @("corvus-update", "osint-workbench-update")) {
    $protocolRoot = "HKCU:\Software\Classes\$scheme"
    New-Item -Path $protocolRoot -Force | Out-Null
    Set-Item -Path $protocolRoot -Value "URL:Corvus Update"
    New-ItemProperty -Path $protocolRoot -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
    if (Test-Path -LiteralPath $icon -PathType Leaf) {
        New-Item -Path (Join-Path $protocolRoot "DefaultIcon") -Force | Out-Null
        Set-Item -Path (Join-Path $protocolRoot "DefaultIcon") -Value $icon
    }
    $commandKey = Join-Path $protocolRoot "shell\open\command"
    New-Item -Path $commandKey -Force | Out-Null
    Set-Item -Path $commandKey -Value $command
}

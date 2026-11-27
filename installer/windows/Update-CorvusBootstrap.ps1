param(
    [string]$InstallRoot = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName PresentationFramework

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
    $git = Find-GitExecutable
    if ($git) { return $git }
    $winget = Find-WingetExecutable
    if (-not $winget) {
        throw "Git for Windows is missing and Windows Package Manager (winget) is unavailable. Install Git for Windows and run Corvus Update again."
    }
    $process = Start-Process -FilePath $winget -ArgumentList @(
        "install", "--id", "Git.Git", "--exact", "--source", "winget",
        "--scope", "user", "--silent", "--accept-source-agreements",
        "--accept-package-agreements", "--disable-interactivity"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Git for Windows installation failed with exit code $($process.ExitCode)."
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    do {
        $git = Find-GitExecutable
        if ($git) { return $git }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Git for Windows installed but git.exe did not become available within 120 seconds."
}

function Resolve-AppRoot {
    $resultPath = Join-Path $InstallRoot "installer-result.ini"
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $resultPath) {
            if ($line -match '^AppRoot=(.+)$') {
                $candidate = $Matches[1].Trim()
                if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
            }
        }
    }
    $candidate = Join-Path $InstallRoot "app"
    if (Test-Path -LiteralPath $candidate -PathType Container) { return $candidate }
    throw "The Corvus application checkout could not be located under $InstallRoot."
}

try {
    [void](Ensure-GitExecutable)
    $appRoot = Resolve-AppRoot
    $guiUpdater = Join-Path $appRoot "Update-CorvusGUI.ps1"
    $updater = Join-Path $appRoot "Update-Corvus.ps1"
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
        $updater = Join-Path $appRoot "Update-OSINTWorkbench.ps1"
    }
    if (-not (Test-Path -LiteralPath $updater -PathType Leaf)) {
        throw "Neither the Corvus updater nor its legacy updater exists in $appRoot. Repair the application checkout before updating."
    }
    $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if (Test-Path -LiteralPath $guiUpdater -PathType Leaf) {
        Start-Process -FilePath $powershell -ArgumentList @(
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", ('"{0}"' -f $guiUpdater), "-UpdaterPath", ('"{0}"' -f $updater)
        ) -WorkingDirectory $appRoot | Out-Null
    } elseif (Test-Path -LiteralPath $updater -PathType Leaf) {
        Start-Process -FilePath $powershell -ArgumentList @(
            "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", ('"{0}"' -f $updater), "-OnlyIfChanged"
        ) -WorkingDirectory $appRoot | Out-Null
    } else {
        throw "Corvus Update files were not found in $appRoot."
    }
} catch {
    [System.Windows.MessageBox]::Show(
        $_.Exception.Message,
        "Corvus Update",
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
    exit 1
}

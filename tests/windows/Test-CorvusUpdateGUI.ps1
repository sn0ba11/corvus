param([string]$ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "This test requires Windows PowerShell with WPF." }
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("Corvus update test " + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
$updater = Join-Path $sandbox "Update-Corvus.ps1"
$originalMode = $env:CORVUS_REPRO_MODE
[IO.File]::WriteAllText($updater, @'
param([switch]$OnlyIfChanged)
if ($env:CORVUS_REPRO_MODE -ne "empty") { [Console]::Error.WriteLine("Receiving objects: controlled progress fixture") }
Write-Output "Controlled updater reached the script under a path containing spaces."
if ($env:CORVUS_REPRO_MODE -eq "error") { exit 7 }
exit 0
'@)
$source = Get-Content -LiteralPath (Join-Path $ProjectRoot "Update-CorvusGUI.ps1") -Raw
$test = @'
$frame = New-Object System.Windows.Threading.DispatcherFrame
$watch = New-Object System.Windows.Threading.DispatcherTimer
$watch.Interval = [TimeSpan]::FromMilliseconds(100)
$deadline = [DateTime]::UtcNow.AddSeconds(20)
$watch.Add_Tick({
    if ($script:process -eq $null -or [DateTime]::UtcNow -gt $deadline) {
        $watch.Stop()
        $frame.Continue = $false
    }
})
$updateButton.RaiseEvent((New-Object System.Windows.RoutedEventArgs([System.Windows.Controls.Button]::ClickEvent)))
$watch.Start()
[System.Windows.Threading.Dispatcher]::PushFrame($frame)
$result = [pscustomobject]@{ Mode=$env:CORVUS_REPRO_MODE; Badge=$stateBadgeText.Text; ButtonEnabled=$updateButton.IsEnabled; ProgressLogged=$logBox.Text.Contains("Receiving objects"); ScriptReached=$logBox.Text.Contains("Controlled updater reached") }
$result | ConvertTo-Json -Compress | Write-Output
$expected = if ($env:CORVUS_REPRO_MODE -eq "error") { "FAILED" } else { "COMPLETE" }
try {
    if ($result.Badge -ne $expected -or -not $result.ScriptReached -or -not $result.ButtonEnabled) {
        throw "GUI did not report the expected native process outcome."
    }
} finally { $window.Close() }
'@
if (-not $source.Contains('[void]$window.ShowDialog()')) { throw "GUI test could not identify its window entrypoint." }
$source = $source.Replace('[void]$window.ShowDialog()', $test)
try {
    foreach ($mode in @("empty", "progress", "error")) {
        $env:CORVUS_REPRO_MODE = $mode
        & ([scriptblock]::Create($source)) -UpdaterPath $updater
    }
} finally {
    $env:CORVUS_REPRO_MODE = $originalMode
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

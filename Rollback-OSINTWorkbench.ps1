$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$root\OSINTWorkbench.ps1" -Command rollback
exit $LASTEXITCODE

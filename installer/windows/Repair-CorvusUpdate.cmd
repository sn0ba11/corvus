@echo off
setlocal
echo Repairing Corvus Update using this installation's configured Git source.
echo Existing case data and Docker volumes are preserved.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-CorvusLauncher.ps1" -OnlyIfChanged %*
set "CORVUS_UPDATE_EXIT=%ERRORLEVEL%"
if not "%CORVUS_UPDATE_EXIT%"=="0" echo Corvus repair failed with exit code %CORVUS_UPDATE_EXIT%. Keep this window open and copy the error above.
pause
exit /b %CORVUS_UPDATE_EXIT%

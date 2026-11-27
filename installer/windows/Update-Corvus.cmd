@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-CorvusLauncher.ps1" %*
set "CORVUS_UPDATE_EXIT=%ERRORLEVEL%"
if not "%CORVUS_UPDATE_EXIT%"=="0" echo Corvus update failed with exit code %CORVUS_UPDATE_EXIT%.
pause
exit /b %CORVUS_UPDATE_EXIT%

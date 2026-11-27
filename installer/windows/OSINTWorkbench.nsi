Unicode True

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"

!ifndef PRODUCT_VERSION
  !define PRODUCT_VERSION "0.2.0-rc12"
!endif
!ifndef SOURCE_REPOSITORY
  !define SOURCE_REPOSITORY "https://github.com/sn0ba11/corvus.git"
!endif
!ifndef PRODUCT_FILE_VERSION
  !define PRODUCT_FILE_VERSION "0.2.0.0"
!endif
!ifndef PROJECT_ROOT
  !define PROJECT_ROOT "..\.."
!endif
!ifndef PAYLOAD_DIR
  !define PAYLOAD_DIR "..\..\dist\windows-installer\payload"
!endif
!ifndef OUTPUT_DIR
  !define OUTPUT_DIR "..\..\dist\release"
!endif
!ifndef BRANDING_DIR
  !define BRANDING_DIR "${PROJECT_ROOT}\installer\windows\branding"
!endif
!ifndef PRODUCT_ICON
  !define PRODUCT_ICON "${BRANDING_DIR}\Corvus.ico"
!endif
!define PRODUCT_WIZARD_BITMAP "${BRANDING_DIR}\Corvus-wizard.bmp"
!define PRODUCT_HEADER_BITMAP "${BRANDING_DIR}\Corvus-header.bmp"

!define PRODUCT_NAME "Corvus"
!define PRODUCT_PUBLISHER "Corvus"
!define PRODUCT_COPYRIGHT "Copyright (c) 2026 sn0ba11"
!define PRODUCT_WEB_SITE "https://github.com/sn0ba11/corvus"
!define PRODUCT_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OSINTWorkbench"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "${OUTPUT_DIR}\Corvus-${PRODUCT_VERSION}-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Corvus"
InstallDirRegKey HKCU "${PRODUCT_UNINSTALL_KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show
BrandingText "Corvus"

VIProductVersion "${PRODUCT_FILE_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=1033 "FileVersion" "${PRODUCT_VERSION}"
VIFileVersion "${PRODUCT_FILE_VERSION}"
VIAddVersionKey /LANG=1033 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1033 "FileDescription" "${PRODUCT_NAME} Setup"
VIAddVersionKey /LANG=1033 "LegalCopyright" "${PRODUCT_COPYRIGHT}"
VIAddVersionKey /LANG=1033 "InternalName" "Corvus-Setup"
VIAddVersionKey /LANG=1033 "OriginalFilename" "Corvus-${PRODUCT_VERSION}-Setup.exe"

Var WorkbenchUrl
Var BrowserUrl
Var AppRoot
Var InstallError
Var PurgeData

!define MUI_ABORTWARNING
!define MUI_CUSTOMFUNCTION_ABORT StopBootstrapOnAbort
!define MUI_ICON "${PRODUCT_ICON}"
!define MUI_UNICON "${PRODUCT_ICON}"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${PRODUCT_WIZARD_BITMAP}"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${PRODUCT_WIZARD_BITMAP}"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${PRODUCT_HEADER_BITMAP}"
!define MUI_HEADERIMAGE_UNBITMAP "${PRODUCT_HEADER_BITMAP}"
!define MUI_WELCOMEPAGE_TITLE "Welcome to Corvus ${PRODUCT_VERSION} Setup"
!define MUI_FINISHPAGE_TITLE "Corvus is ready"
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Corvus will be removed from your computer."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open Corvus"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchWorkbench
!define MUI_FINISHPAGE_LINK "Open the installation folder"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${PROJECT_ROOT}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Function .onInit
  StrCpy $WorkbenchUrl "http://127.0.0.1:8000"
  StrCpy $BrowserUrl "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
  StrCpy $AppRoot "$INSTDIR\app"
FunctionEnd

Function LaunchWorkbench
  ExecShell "open" "$WorkbenchUrl"
FunctionEnd

Function StopBootstrapOnAbort
  ClearErrors
  FileOpen $0 "$INSTDIR\logs\bootstrap.pid" r
  ${IfNot} ${Errors}
    FileRead $0 $1
    FileClose $0
    ${If} $1 != ""
      DetailPrint "Stopping the active Corvus setup process..."
      ExecWait '"$SYSDIR\taskkill.exe" /PID $1 /T /F'
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.onInit
  StrCpy $PurgeData "0"
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/PURGE" $1
  ${IfNot} ${Errors}
    StrCpy $PurgeData "1"
    Return
  ${EndIf}

  MessageBox MB_ICONEXCLAMATION|MB_YESNOCANCEL \
      "Choose the uninstall mode.$\r$\n$\r$\nYES — FULL FACTORY RESET$\r$\nDelete all Corvus containers, service images, networks, investigations, browser sessions and evidence volumes.$\r$\n$\r$\nNO — REMOVE PROGRAM ONLY$\r$\nStop containers but preserve Docker volumes for later reinstallation.$\r$\n$\r$\nCANCEL — Exit without changing anything." \
    IDYES full_reset IDNO preserve_data
  Abort

  full_reset:
    StrCpy $PurgeData "1"
    Goto mode_selected

  preserve_data:
    StrCpy $PurgeData "0"
    Goto mode_selected

  mode_selected:
FunctionEnd

Section "Install Corvus" SEC_MAIN
  SetShellVarContext current
  SetOutPath "$INSTDIR\installer"
  File /oname=Bootstrap-OSINTWorkbench.ps1 "${PROJECT_ROOT}\installer\windows\Bootstrap-OSINTWorkbench.ps1"
  File /oname=Update-CorvusBootstrap.ps1 "${PROJECT_ROOT}\installer\windows\Update-CorvusBootstrap.ps1"
  File /oname=Uninstall-OSINTWorkbench.ps1 "${PROJECT_ROOT}\installer\windows\Uninstall-OSINTWorkbench.ps1"
  File /oname=OSINTWorkbench.ico "${PRODUCT_ICON}"
  File /oname=LICENSE.txt "${PROJECT_ROOT}\LICENSE"
  File /oname=COPYRIGHT.txt "${PROJECT_ROOT}\COPYRIGHT"
  File /oname=THIRD_PARTY_NOTICES.txt "${PROJECT_ROOT}\THIRD_PARTY_NOTICES.md"

  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=osint-workbench.bundle "${PAYLOAD_DIR}\osint-workbench.bundle"
  IfFileExists "$PLUGINSDIR\osint-workbench.bundle" bundle_ready 0
    MessageBox MB_ICONSTOP|MB_OK "The embedded Corvus source bundle could not be extracted.$\r$\n$\r$\nSetup cannot continue." /SD IDOK
    SetErrorLevel 1
    Abort
  bundle_ready:

  DetailPrint "Checking prerequisites, installing the application checkout and starting Docker services..."
  ${If} ${Silent}
    ; The bootstrap writes a transcript. Silent mode has no details list for the build output.
    ClearErrors
    ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\installer\Bootstrap-OSINTWorkbench.ps1" -BundlePath "$PLUGINSDIR\osint-workbench.bundle" -InstallRoot "$INSTDIR" -RepositoryUrl "${SOURCE_REPOSITORY}"' $0
    ${If} ${Errors}
      StrCpy $0 1
    ${EndIf}
  ${Else}
    GetDlgItem $2 $HWNDPARENT 2
    EnableWindow $2 1
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\installer\Bootstrap-OSINTWorkbench.ps1" -BundlePath "$PLUGINSDIR\osint-workbench.bundle" -InstallRoot "$INSTDIR" -RepositoryUrl "${SOURCE_REPOSITORY}"'
    Pop $0
  ${EndIf}
  ${If} $0 != 0
    StrCpy $InstallError "Corvus setup did not complete."
    ClearErrors
    FileOpen $1 "$INSTDIR\logs\install-error.txt" r
    ${IfNot} ${Errors}
      FileRead $1 $InstallError
      FileClose $1
    ${EndIf}
    MessageBox MB_ICONSTOP|MB_OK "$InstallError$\r$\n$\r$\nDetailed log:$\r$\n$INSTDIR\logs\install.log" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}

  ReadINIStr $AppRoot "$INSTDIR\installer-result.ini" "OSINTWorkbench" "AppRoot"
  ReadINIStr $WorkbenchUrl "$INSTDIR\installer-result.ini" "OSINTWorkbench" "WorkbenchUrl"
  ReadINIStr $BrowserUrl "$INSTDIR\installer-result.ini" "OSINTWorkbench" "BrowserUrl"
  ${If} $AppRoot == ""
    StrCpy $AppRoot "$INSTDIR\app"
  ${EndIf}
  ${If} $WorkbenchUrl == ""
    StrCpy $WorkbenchUrl "http://127.0.0.1:8000"
  ${EndIf}
  ${If} $BrowserUrl == ""
    StrCpy $BrowserUrl "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"
  ${EndIf}

  CreateDirectory "$SMPROGRAMS\Corvus"
  Delete "$SMPROGRAMS\Corvus\Corvus.url"
  CreateShortCut "$SMPROGRAMS\Corvus\Corvus.lnk" "$WINDIR\explorer.exe" "$WorkbenchUrl" "$INSTDIR\installer\OSINTWorkbench.ico" 0 SW_SHOWNORMAL
  Delete "$SMPROGRAMS\Corvus\Check for Updates.lnk"
  CreateShortCut "$SMPROGRAMS\Corvus\Corvus Update.lnk" "$WINDIR\explorer.exe" "corvus-update://run" "$INSTDIR\installer\OSINTWorkbench.ico" 0 SW_SHOWNORMAL
  WriteRegStr HKCU "Software\Classes\corvus-update" "" "URL:Corvus Update"
  WriteRegStr HKCU "Software\Classes\corvus-update" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\corvus-update\DefaultIcon" "" "$INSTDIR\installer\OSINTWorkbench.ico"
  WriteRegStr HKCU "Software\Classes\corvus-update\shell\open\command" "" '$\"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe$\" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $\"$INSTDIR\installer\Update-CorvusBootstrap.ps1$\" -InstallRoot $\"$INSTDIR$\"'
  WriteRegStr HKCU "Software\Classes\osint-workbench-update" "" "URL:Corvus Update (compatibility)"
  WriteRegStr HKCU "Software\Classes\osint-workbench-update" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\osint-workbench-update\DefaultIcon" "" "$INSTDIR\installer\OSINTWorkbench.ico"
  WriteRegStr HKCU "Software\Classes\osint-workbench-update\shell\open\command" "" '$\"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe$\" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $\"$INSTDIR\installer\Update-CorvusBootstrap.ps1$\" -InstallRoot $\"$INSTDIR$\"'

  Delete "$DESKTOP\Corvus.url"
  Delete "$DESKTOP\Corvus Sock Browser.url"
  Delete "$DESKTOP\OSINT Sock Browser.url"
  CreateShortCut "$DESKTOP\Corvus.lnk" "$WINDIR\explorer.exe" "$WorkbenchUrl" "$INSTDIR\installer\OSINTWorkbench.ico" 0 SW_SHOWNORMAL

  WriteUninstaller "$INSTDIR\Uninstall Corvus.exe"
  CreateShortCut "$SMPROGRAMS\Corvus\Uninstall Corvus.lnk" "$INSTDIR\Uninstall Corvus.exe" "" "$INSTDIR\installer\OSINTWorkbench.ico" 0
  CreateShortCut "$SMPROGRAMS\Corvus\Factory Reset Corvus.lnk" "$INSTDIR\Uninstall Corvus.exe" "/PURGE" "$INSTDIR\installer\OSINTWorkbench.ico" 0

  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "HelpLink" "${PRODUCT_WEB_SITE}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\installer\OSINTWorkbench.ico"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall Corvus.exe"'
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ${If} $PurgeData == "1"
    DetailPrint "Performing a full Corvus factory reset..."
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\installer\Uninstall-OSINTWorkbench.ps1" -InstallRoot "$INSTDIR" -PurgeData'
  ${Else}
    DetailPrint "Removing the application while preserving Corvus Docker volumes..."
    nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\installer\Uninstall-OSINTWorkbench.ps1" -InstallRoot "$INSTDIR"'
  ${EndIf}
  Pop $0
  ${If} $0 != 0
    StrCpy $InstallError "Corvus cleanup did not complete."
    ClearErrors
    FileOpen $1 "$INSTDIR\logs\uninstall-error.txt" r
    ${IfNot} ${Errors}
      FileRead $1 $InstallError
      FileClose $1
    ${EndIf}
    MessageBox MB_ICONSTOP|MB_OK "$InstallError$\r$\n$\r$\nStart Docker Desktop and retry.$\r$\nDetailed log:$\r$\n$INSTDIR\logs\uninstall.log"
    Abort
  ${EndIf}

  Delete "$DESKTOP\Corvus.url"
  Delete "$DESKTOP\Corvus.lnk"
  Delete "$DESKTOP\OSINT Workbench.url"
  Delete "$DESKTOP\OSINT Sock Browser.url"
  RMDir /r "$SMPROGRAMS\Corvus"
  RMDir /r "$SMPROGRAMS\OSINT Workbench"
  DeleteRegKey HKCU "Software\Classes\corvus-update"
  DeleteRegKey HKCU "Software\Classes\osint-workbench-update"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd

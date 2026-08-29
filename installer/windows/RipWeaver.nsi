Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma

!include "LogicLib.nsh"
!include "MUI2.nsh"
!include "nsDialogs.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.0.0"
!endif
!ifndef SOURCE_DIR
  !define SOURCE_DIR "..\..\dist\RipWeaver"
!endif
!ifndef OUTPUT_FILE
  !define OUTPUT_FILE "..\..\dist\RipWeaver-Setup-Windows-x64.exe"
!endif
!ifndef LICENSE_FILE
  !define LICENSE_FILE "..\..\LICENSE"
!endif

!define PRODUCT_NAME "RipWeaver"
!define PRODUCT_PUBLISHER "RipWeaver"
!define PRODUCT_WEB_SITE "https://ripweaver.com"
!define PRODUCT_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\RipWeaver"

Name "${PRODUCT_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\RipWeaver"
InstallDirRegKey HKCU "Software\RipWeaver" "InstallDir"
ShowInstDetails show
ShowUninstDetails show

Var PrerequisiteDialog

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\RipWeaver.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start RipWeaver"
!define MUI_FINISHPAGE_LINK "Visit ripweaver.com"
!define MUI_FINISHPAGE_LINK_LOCATION "${PRODUCT_WEB_SITE}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${LICENSE_FILE}"
Page custom PrerequisitesPage
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Function PrerequisitesPage
  !insertmacro MUI_HEADER_TEXT "External tools" "Install only the tools needed for your workflow"
  nsDialogs::Create 1018
  Pop $PrerequisiteDialog
  ${If} $PrerequisiteDialog == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 32u "RipWeaver does not bundle or install these independent products. The links below open each project's official download page. Review and accept each product's own license yourself."
  Pop $0

  ${NSD_CreateLink} 0 42u 100% 12u "MakeMKV — disc scanning and ripping"
  Pop $0
  ${NSD_OnClick} $0 OpenMakeMKV

  ${NSD_CreateLink} 0 60u 100% 12u "HandBrakeCLI — video transcoding"
  Pop $0
  ${NSD_OnClick} $0 OpenHandBrake

  ${NSD_CreateLink} 0 78u 100% 12u "FFmpeg and FFprobe — media analysis and verification"
  Pop $0
  ${NSD_OnClick} $0 OpenFFmpeg

  ${NSD_CreateLabel} 0 102u 100% 30u "You may continue without these tools. RipWeaver's Setup & Health screen will explain which features are available and link back to these official pages."
  Pop $0

  nsDialogs::Show
FunctionEnd

Function OpenMakeMKV
  ExecShell "open" "https://www.makemkv.com/download/"
FunctionEnd

Function OpenHandBrake
  ExecShell "open" "https://handbrake.fr/downloads2.php"
FunctionEnd

Function OpenFFmpeg
  ExecShell "open" "https://ffmpeg.org/download.html"
FunctionEnd

Section "RipWeaver" SectionMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Replace only files owned by the previous RipWeaver installation. Local
  ; configuration and private pipeline state live outside this directory.
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\RipWeaver.exe"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.txt"
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\RipWeaver" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\RipWeaver.exe"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoRepair" 1

  CreateDirectory "$SMPROGRAMS\RipWeaver"
  CreateShortcut "$SMPROGRAMS\RipWeaver\RipWeaver.lnk" "$INSTDIR\RipWeaver.exe"
  CreateShortcut "$SMPROGRAMS\RipWeaver\Uninstall RipWeaver.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\RipWeaver\RipWeaver.lnk"
  Delete "$SMPROGRAMS\RipWeaver\Uninstall RipWeaver.lnk"
  RMDir "$SMPROGRAMS\RipWeaver"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\RipWeaver.exe"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\RipWeaver"
SectionEnd

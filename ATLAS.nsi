; ATLAS Installer Script (NSIS)
; Build: makensis ATLAS.nsi   (after dist\ATLAS\ is produced by ATLAS.spec)
; Output: dist\ATLAS_Setup.exe
;
; This is a PER-USER installer:
;   * No admin elevation required (no UAC prompt, no Defender install warning).
;   * Installs to %LOCALAPPDATA%\Programs\ATLAS (the modern Windows convention
;     used by VS Code, Chrome, etc. -- writable without elevation).
;   * All registry writes target HKCU, consistent with the install location,
;     so uninstall shows up correctly in Apps & Features for the user who
;     actually installed it.

!include "MUI2.nsh"

; ---- Version (single source of truth) -------------------------------------
; Keep ATLAS_VERSION in lockstep with config.APP_VERSION in the Python source.
; ATLAS_VERSION_4PART is the Windows MAJOR.MINOR.PATCH.BUILD form required by
; VIProductVersion; build number stays at 0.
!define ATLAS_VERSION "2.0.10.0"
!define ATLAS_VERSION_4PART "2.0.10.0"

; ---- Basic settings --------------------------------------------------------
Name "ATLAS"
OutFile "dist\ATLAS_Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\ATLAS"
InstallDirRegKey HKCU "Software\ATLAS" ""
Icon "icon.ico"
UninstallIcon "icon.ico"

; Per-user install -- no admin needed
RequestExecutionLevel user
SetCompressor /SOLID lzma

; ---- MUI pages -------------------------------------------------------------
!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\ATLAS.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ATLAS now"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ---- Version metadata ------------------------------------------------------
VIProductVersion "${ATLAS_VERSION_4PART}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductName"      "ATLAS"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductVersion"   "${ATLAS_VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileVersion"      "${ATLAS_VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "CompanyName"      "LightRiver Technologies"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileDescription"  "Automated Toolkit for LightRiver Asset & Systems"
VIAddVersionKey /LANG=${LANG_ENGLISH} "LegalCopyright"   "(C) 2026 LightRiver Technologies"

; ---- Install ---------------------------------------------------------------
Section "Install ATLAS"
    SetOutPath "$INSTDIR"

    ; Verify the build produced the executable before we commit to the copy.
    ; TDS no longer ships as a separate binary -- ATLAS.exe handles both
    ; roles via --tds-mode (see main.py).
    !if /FileExists "dist\ATLAS\ATLAS.exe"
    !else
        !error "dist\ATLAS\ATLAS.exe missing -- run build.bat before makensis."
    !endif

    ; Copy the entire onedir output (ATLAS.exe, TDS.exe, _internal\, data\)
    File /r "dist\ATLAS\*.*"

    ; Shortcuts
    CreateDirectory "$SMPROGRAMS\ATLAS"
    CreateShortCut  "$SMPROGRAMS\ATLAS\ATLAS.lnk"     "$INSTDIR\ATLAS.exe"     "" "$INSTDIR\ATLAS.exe" 0
    CreateShortCut  "$SMPROGRAMS\ATLAS\Uninstall.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0
    CreateShortCut  "$DESKTOP\ATLAS.lnk"              "$INSTDIR\ATLAS.exe"     "" "$INSTDIR\ATLAS.exe" 0

    ; Per-user registry footprint
    WriteRegStr HKCU "Software\ATLAS" "" "$INSTDIR"
    WriteRegStr HKCU "Software\ATLAS" "Version" "${ATLAS_VERSION}"

    ; Apps & Features (per-user uninstall entry)
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "DisplayName"     "ATLAS"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "DisplayVersion"  "${ATLAS_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "Publisher"       "LightRiver Technologies"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "DisplayIcon"     "$INSTDIR\ATLAS.exe"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS" "NoRepair" 1

    SetAutoClose true
SectionEnd

; ---- Uninstall -------------------------------------------------------------
Section "Uninstall"
    ; NOTE: this only removes files we installed. Per-user state in
    ; %APPDATA%\ATLAS\ (logs, credentials, known_hosts, network_inventory.db)
    ; is intentionally preserved so reinstalling doesn't lose saved devices.
    RMDir /r "$INSTDIR"

    RMDir /r "$SMPROGRAMS\ATLAS"
    Delete  "$DESKTOP\ATLAS.lnk"

    DeleteRegKey HKCU "Software\ATLAS"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ATLAS"

    SetAutoClose true
SectionEnd

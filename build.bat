@echo off
REM ATLAS Build Script
REM
REM Drives PyInstaller using ATLAS.spec (the source of truth for hidden
REM imports, data files, and the second TDS.exe target) and then NSIS.
REM
REM Signing is ALWAYS ON by default using certs\LightRiver_codesign.pfx.
REM Drop the .pfx at that path once (gitignored) and every build will
REM sign ATLAS.exe and ATLAS_Setup.exe. sign.bat prompts for the
REM certificate password interactively each run.
REM
REM Usage:
REM   build.bat                            -- Build + sign exe + installer
REM   build.bat --clean                    -- Wipe build/ and dist/ first
REM   build.bat --no-sign                  -- Skip signing (for debug or CI
REM                                           without cert)
REM   build.bat --sign other.pfx           -- Sign with a non-default cert
REM                                           (overrides certs\LightRiver_codesign.pfx)
REM   build.bat --release                  -- After installer build, remove
REM                                           dist\ATLAS\ (keep only Setup.exe).
REM                                           Default keeps dist\ATLAS\ so you
REM                                           can smoke-test the unpacked app.

setlocal enabledelayedexpansion

REM ---- Pick interpreter (prefer the project venv) -----------------------
if exist ".venv\Scripts\python.exe" (
    set PYTHON=.venv\Scripts\python.exe
) else (
    set PYTHON=python
)

REM ---- Constants --------------------------------------------------------
set APP_NAME=ATLAS
set SPEC_FILE=ATLAS.spec
set NSI_FILE=ATLAS.nsi

REM ---- Parse arguments --------------------------------------------------
REM DO_SIGN defaults to 1 — every build signs unless --no-sign is passed.
REM CERT_FILE defaults to certs\LightRiver_codesign.pfx (gitignored). Pass
REM --sign other.pfx to override.
set DO_CLEAN=0
set DO_SIGN=1
set DO_RELEASE=0
set CERT_FILE=certs\LightRiver_codesign.pfx

:parse_args
if "%1"=="--clean" (
    set DO_CLEAN=1
    shift
    goto parse_args
)
if "%1"=="--sign" (
    set DO_SIGN=1
    set CERT_FILE=%2
    shift
    shift
    goto parse_args
)
if "%1"=="--no-sign" (
    set DO_SIGN=0
    shift
    goto parse_args
)
if "%1"=="--release" (
    set DO_RELEASE=1
    shift
    goto parse_args
)

REM ---- Optional clean ---------------------------------------------------
if "%DO_CLEAN%"=="1" (
    echo [*] Cleaning previous builds...
    if exist dist (
        rmdir /s /q dist
        echo     - dist\ removed
    )
    if exist build (
        rmdir /s /q build
        echo     - build\ removed
    )
)

REM ---- Verify spec file exists -----------------------------------------
if not exist "%SPEC_FILE%" (
    echo [!] ERROR: %SPEC_FILE% not found in current directory.
    echo     The spec file is the source of truth for this build.
    exit /b 1
)

REM ---- Verify dependencies ---------------------------------------------
echo.
echo [*] Checking PyInstaller...
%PYTHON% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [!] PyInstaller not installed. Installing...
    %PYTHON% -m pip install pyinstaller
    if errorlevel 1 (
        echo [!] Failed to install PyInstaller.
        exit /b 1
    )
)

REM ---- Verify bundled data files exist ---------------------------------
echo [*] Verifying bundled assets...
if not exist "ATLAS Logo.png" (
    echo [!] ERROR: "ATLAS Logo.png" not found in project root.
    exit /b 1
)
if not exist "icon.ico" (
    echo [!] WARNING: icon.ico not found - the build will fail because the
    echo            spec references it. Place a 256x256 icon.ico in the
    echo            project root.
    exit /b 1
)
for %%F in (
    "data\ATLAS_Packing_Slip.xlsx"
    "data\ATLAS_Consolidated_Packing_Slip.xlsx"
    "data\Device_Report_Template.xlsx"
    "data\Ciena_RLS_Packing_Slip.xlsx"
    "data\Ciena_RLS_Report_Template.xlsx"
    "data\Nokia_PSI_Packing_Slip.xlsx"
    "data\Nokia_PSI_Report_Template.xlsx"
    "data\network_inventory.db"
) do (
    if not exist %%F (
        echo [!] ERROR: missing required asset: %%F
        exit /b 1
    )
)
echo [OK] All assets present.

REM ---- Build with PyInstaller via the spec -----------------------------
echo.
echo [*] Building %APP_NAME% (and TDS subprocess) via %SPEC_FILE% ...
echo     This takes ~1-2 minutes on first run.
echo.
%PYTHON% -m PyInstaller --clean --noconfirm "%SPEC_FILE%"
if errorlevel 1 (
    echo [!] PyInstaller build failed.
    exit /b 1
)

if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
    echo [!] ERROR: dist\%APP_NAME%\%APP_NAME%.exe was not produced.
    exit /b 1
)

echo.
echo [OK] Executable built: dist\%APP_NAME%\%APP_NAME%.exe
echo      TDS now runs through ATLAS.exe --tds-mode ^(no separate TDS.exe^).

REM ---- Sign the executables before packaging --------------------------
REM Signing is on by default; pass --no-sign to skip (debug / CI builds).
if "%DO_SIGN%"=="1" (
    if not exist "%CERT_FILE%" (
        echo.
        echo [!] Code-signing certificate not found:
        echo         %CERT_FILE%
        echo     Drop the .pfx at that path to enable auto-signing, or
        echo     re-run with --no-sign to skip signing for this build.
        exit /b 1
    )
    echo.
    echo [*] Signing executables with %CERT_FILE% ...
    call sign.bat "%CERT_FILE%" --exe-only
    if errorlevel 1 (
        echo [!] Signing failed - aborting installer build.
        exit /b 1
    )
)

REM ---- Build installer via NSIS ----------------------------------------
echo.
echo [*] Building installer with NSIS ...
where makensis >nul 2>&1
if not errorlevel 1 goto :invoke_nsis_system
if exist "C:\Program Files (x86)\NSIS\makensis.exe" goto :invoke_nsis_x86
if exist "C:\Program Files\NSIS\makensis.exe" goto :invoke_nsis_x64
echo [!] NSIS not found. Install from https://nsis.sourceforge.io/Download
echo     or `choco install nsis -y` and re-run.
exit /b 1

:invoke_nsis_x86
"C:\Program Files (x86)\NSIS\makensis.exe" %NSI_FILE%
goto :nsis_check

:invoke_nsis_x64
"C:\Program Files\NSIS\makensis.exe" %NSI_FILE%
goto :nsis_check

:invoke_nsis_system
makensis %NSI_FILE%

:nsis_check
if errorlevel 1 (
    echo [!] NSIS build failed.
    exit /b 1
)

echo [OK] Installer built: dist\%APP_NAME%_Setup.exe

REM ---- Optionally sign the installer too -------------------------------
if "%DO_SIGN%"=="1" (
    echo [*] Signing installer...
    call sign.bat "%CERT_FILE%" --installer-only
    if errorlevel 1 (
        echo [!] Installer signing failed.
        exit /b 1
    )
)

REM ---- Release mode: drop the unpacked dist\ATLAS\ folder --------------
REM By default we keep it so you can smoke-test dist\ATLAS\ATLAS.exe before
REM running the installer. Pass --release to strip it for distribution.
if "%DO_RELEASE%"=="1" (
    echo.
    echo [*] --release: removing intermediate dist\%APP_NAME%\ folder ...
    if exist "dist\%APP_NAME%" (
        rmdir /s /q "dist\%APP_NAME%"
        echo     - dist\%APP_NAME%\ removed
    )
)

REM ---- Compute SHA-256 of the installer for the release notes ----------
REM Use a subroutine to dodge cmd's "parse-the-whole-block-up-front" quirk
REM that blanks delayed-expansion vars set inside nested for/if blocks.
set INSTALLER_HASH=
if exist "dist\%APP_NAME%_Setup.exe" call :compute_hash "dist\%APP_NAME%_Setup.exe"

echo.
echo ============================================
echo Build Summary
echo ============================================
if exist dist\%APP_NAME%_Setup.exe echo Installer  : dist\%APP_NAME%_Setup.exe
if exist dist\%APP_NAME%\%APP_NAME%.exe echo Unpacked   : dist\%APP_NAME%\%APP_NAME%.exe   ^(run to smoke-test^)
if "%DO_SIGN%"=="1" (echo Signed     : YES  ^(%CERT_FILE%^)) else (echo Signed     : NO  ^(--no-sign was passed^))
if defined INSTALLER_HASH (
    echo SHA-256    : !INSTALLER_HASH!
    echo.
    echo --- Paste into the GitHub release body -----
    echo %APP_NAME%_Setup.exe sha256: !INSTALLER_HASH!
    echo --------------------------------------------
)
echo ============================================
echo.

endlocal
goto :eof

REM ---- Subroutine: hash the installer and stash result in INSTALLER_HASH
:compute_hash
REM findstr already strips the "SHA256 hash of file:" header AND the
REM "CertUtil:" trailer, so do NOT add skip=1 on top — that would
REM swallow the lone hash line.
for /f "tokens=*" %%H in ('certutil -hashfile %1 SHA256 ^| findstr /v ":"') do (
    if not defined INSTALLER_HASH set "INSTALLER_HASH=%%H"
)
if defined INSTALLER_HASH set "INSTALLER_HASH=!INSTALLER_HASH: =!"
exit /b 0

@echo off
REM ATLAS Code Signing Script
REM Signs ATLAS.exe and/or ATLAS_Setup.exe with a code-signing cert.
REM
REM Usage:
REM   sign.bat <cert.pfx>                  -- Sign ATLAS.exe + installer
REM   sign.bat <cert.pfx> --exe-only       -- Sign only ATLAS.exe
REM   sign.bat <cert.pfx> --installer-only -- Sign only the installer
REM
REM Requires: signtool.exe (ships with Windows SDK / Visual Studio Build Tools)

setlocal enabledelayedexpansion

REM ---- Parse args -------------------------------------------------------
set CERT_FILE=%1
set MODE=both
if "%2"=="--exe-only"       set MODE=exe
if "%2"=="--installer-only" set MODE=installer

if "%CERT_FILE%"=="" (
    echo.
    echo Usage: sign.bat ^<path_to_certificate.pfx^> [--exe-only ^| --installer-only]
    echo.
    echo Examples:
    echo   sign.bat "certs\LightRiver_codesign.pfx"
    echo   sign.bat "certs\LightRiver_codesign.pfx" --exe-only
    echo   sign.bat "certs\LightRiver_codesign.pfx" --installer-only
    echo.
    exit /b 1
)

if not exist "%CERT_FILE%" (
    echo [!] ERROR: Certificate not found: %CERT_FILE%
    exit /b 1
)

REM ---- Locate signtool --------------------------------------------------
signtool /? >nul 2>&1
if errorlevel 1 (
    set SIGNTOOL=
    for %%d in (
        "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
        "C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64\signtool.exe"
        "C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe"
    ) do (
        if exist %%d set SIGNTOOL=%%d
    )
    if "!SIGNTOOL!"=="" (
        echo [!] ERROR: signtool.exe not found on PATH or in Windows Kits.
        echo     Install Windows SDK: choco install windows-sdk -y
        exit /b 1
    )
) else (
    set SIGNTOOL=signtool
)

REM ---- Certificate password --------------------------------------------
set /p CERT_PASSWORD="Enter certificate password: "

REM ---- Signing parameters ----------------------------------------------
set TIMESTAMP_SERVER=http://timestamp.digicert.com
set PUBLISHER_DESC=ATLAS - Automated Toolkit for LightRiver Asset and Systems
set PUBLISHER_URL=https://www.lightrivertechnologies.com

REM ---- Dispatch ---------------------------------------------------------
if "%MODE%"=="installer" goto :sign_installer

REM ---- Sign ATLAS.exe ---------------------------------------------------
:sign_exe
if not exist "dist\ATLAS\ATLAS.exe" (
    echo [!] ERROR: dist\ATLAS\ATLAS.exe not found - run build.bat first.
    exit /b 1
)

echo [*] Signing dist\ATLAS\ATLAS.exe ...
%SIGNTOOL% sign ^
  /f "%CERT_FILE%" ^
  /p "%CERT_PASSWORD%" ^
  /fd SHA256 ^
  /tr "%TIMESTAMP_SERVER%" ^
  /td SHA256 ^
  /d "%PUBLISHER_DESC%" ^
  /du "%PUBLISHER_URL%" ^
  dist\ATLAS\ATLAS.exe
if errorlevel 1 (
    echo [!] Failed to sign ATLAS.exe
    exit /b 1
)
echo [OK] ATLAS.exe signed.

if "%MODE%"=="exe" goto :verify

REM ---- Sign installer ---------------------------------------------------
:sign_installer
if not exist "dist\ATLAS_Setup.exe" (
    echo [!] ERROR: dist\ATLAS_Setup.exe not found - run makensis ATLAS.nsi first.
    exit /b 1
)
echo [*] Signing dist\ATLAS_Setup.exe ...
%SIGNTOOL% sign ^
  /f "%CERT_FILE%" ^
  /p "%CERT_PASSWORD%" ^
  /fd SHA256 ^
  /tr "%TIMESTAMP_SERVER%" ^
  /td SHA256 ^
  /d "%PUBLISHER_DESC%" ^
  /du "%PUBLISHER_URL%" ^
  dist\ATLAS_Setup.exe
if errorlevel 1 (
    echo [!] Failed to sign ATLAS_Setup.exe
    exit /b 1
)
echo [OK] ATLAS_Setup.exe signed.

REM ---- Verify -----------------------------------------------------------
:verify
echo.
echo [*] Verifying signatures...
if "%MODE%"=="both" (
    %SIGNTOOL% verify /pa /v dist\ATLAS\ATLAS.exe
    %SIGNTOOL% verify /pa /v dist\ATLAS_Setup.exe
) else if "%MODE%"=="exe" (
    %SIGNTOOL% verify /pa /v dist\ATLAS\ATLAS.exe
) else (
    %SIGNTOOL% verify /pa /v dist\ATLAS_Setup.exe
)

echo.
echo [OK] Signing complete.
echo.

endlocal

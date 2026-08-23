@echo off
REM ATLAS update smoke test
REM
REM Points the installed ATLAS at a staging GitHub repo so you can confirm the
REM auto-update flow end-to-end without touching the production release feed.
REM
REM Usage:
REM   tools\verify_update.bat <owner>/<repo>
REM
REM Example:
REM   tools\verify_update.bat Chees3loaf/LAMIS-staging
REM
REM What this does:
REM   1. Sets LAMIS_UPDATE_REPO so utils.update.Updater queries your staging
REM      repo instead of the production one.
REM   2. Launches the installed ATLAS.exe under that override.
REM
REM Workflow to validate the pipeline:
REM   a) Build ATLAS_Setup.exe with config.APP_VERSION=2.0.1 (current).
REM   b) Run the installer; confirm v2.0.1 is the running build.
REM   c) Bump config.APP_VERSION=2.0.2, rebuild, and publish v2.0.2 to your
REM      staging repo with ATLAS_Setup.exe attached.
REM   d) Run this script. ATLAS launches, the boot probe queries staging,
REM      and the update dialog should appear within ~1 second.
REM   e) Click Yes; confirm the installer runs and v2.0.2 relaunches.
REM
REM Clean-up:
REM   Close ATLAS and delete the staging release. No state is persisted
REM   anywhere — the env var is process-scoped.

setlocal

if "%~1"=="" (
    echo Usage: %~nx0 ^<owner^>/^<repo^>
    echo Example: %~nx0 Chees3loaf/LAMIS-staging
    exit /b 1
)

set "LAMIS_UPDATE_REPO=%~1"
echo [verify_update] Pointing ATLAS at %LAMIS_UPDATE_REPO%

set "ATLAS_EXE=%LOCALAPPDATA%\Programs\ATLAS\ATLAS.exe"
if not exist "%ATLAS_EXE%" (
    echo [verify_update] ATLAS.exe not found at:
    echo                 %ATLAS_EXE%
    echo                 Install ATLAS first via dist\ATLAS_Setup.exe.
    exit /b 1
)

echo [verify_update] Launching: %ATLAS_EXE%
start "" "%ATLAS_EXE%"

endlocal

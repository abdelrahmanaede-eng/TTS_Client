@echo off
:: Delayed expansion stays off on purpose: certificate passwords may contain "!".
setlocal
title TimeTracker - Build Installer

echo ========================================================
echo       TimeTracker - Automated Build and Setup Generator
echo ========================================================
echo.

cd /d "%~dp0"

:: 1. Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH!
    pause
    exit /b 1
)

:: 2. Find Inno Setup Compiler
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" (
    set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
) else (
    where iscc >nul 2>&1
    if not errorlevel 1 (
        set "ISCC=iscc"
    )
)

if "%ISCC%"=="" (
    echo [ERROR] Inno Setup 6 was not found!
    echo.
    echo Nothing was built: dist\ was left untouched and no installer was produced.
    echo Install it with: winget install JRSoftware.InnoSetup
    echo then run this script again. See dist\README_STALE.txt.
    exit /b 2
)

:: 3. Version prompt and update
:ask_version
set "CURRENT_VER="
for /f "delims=" %%v in ('python scripts\set_version.py --get') do set "CURRENT_VER=%%v"

if not defined CURRENT_VER (
    echo [ERROR] Could not read APP_VERSION from config.py!
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   Current Version in config.py: %CURRENT_VER%
echo ========================================================
if not "%~1"=="" (
    set "INPUT_VER=%~1"
    echo Using version from parameter: %~1
    shift
    goto :process_version
)

set "INPUT_VER="
set /p "INPUT_VER=Enter new version (Press Enter to keep %CURRENT_VER%): "

:process_version
if not defined INPUT_VER goto :keep_version
for /f "tokens=* delims= " %%a in ("%INPUT_VER%") do set "INPUT_VER=%%a"
if "%INPUT_VER%"=="" goto :keep_version

python scripts\set_version.py "%INPUT_VER%" >nul
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to set version. Please try again.
    goto :ask_version
)

for /f "delims=" %%v in ('python scripts\set_version.py --get') do set "APP_VER=%%v"
echo [OK] Version successfully updated to %APP_VER% in config.py!
goto :version_confirmed

:keep_version
set "APP_VER=%CURRENT_VER%"
echo [INFO] Keeping current version: %APP_VER%

:version_confirmed
echo.

echo [1/4] Ensuring icon is generated...
if not exist "assets\setup_icon.ico" (
    python -c "from PIL import Image; img = Image.open('assets/icon_active.png').convert('RGBA'); img.save('assets/setup_icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48)], bitmap_format='bmp')"
)

echo [2/4] Closing any running TimeTracker instance (if any)...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\close_running_instance.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Aborting build: dist\TimeTracker.exe could not be unlocked.
    pause
    exit /b 1
)

echo [2/4] Building TimeTracker.exe with PyInstaller...
python -m PyInstaller --clean --noconfirm TimeTracker.spec
if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller build failed!
    pause
    exit /b 1
)

:: ------------------------------------------------------------------
:: 3. Optional Authenticode code signing.
::    SEC-08: prefer SIGN_CERT_THUMBPRINT (certificate already imported into
::    the CurrentUser store) so no password ever appears in argv or build logs.
::    SIGN_CERT_PATH + SIGN_CERT_PASSWORD still work but are deprecated and
::    warn loudly. Signing is skipped when neither configuration is present.
:: ------------------------------------------------------------------
set "SIGN_ARGS="
set "SIGNTOOL="
set "SIGN_MODE="
if defined SIGN_CERT_THUMBPRINT set "SIGN_MODE=thumbprint"
if not defined SIGN_MODE if defined SIGN_CERT_PATH if defined SIGN_CERT_PASSWORD set "SIGN_MODE=pfx"
if not defined SIGN_MODE goto :no_signing

where signtool >nul 2>&1
if not errorlevel 1 set "SIGNTOOL=signtool"
if not defined SIGNTOOL if exist "%ProgramFiles(x86)%\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe" set "SIGNTOOL=%ProgramFiles(x86)%\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
if not defined SIGNTOOL if exist "%ProgramFiles(x86)%\Windows Kits\10\bin\x64\signtool.exe" set "SIGNTOOL=%ProgramFiles(x86)%\Windows Kits\10\bin\x64\signtool.exe"
if not defined SIGNTOOL if exist "%ProgramFiles%\Windows Kits\10\bin\x64\signtool.exe" set "SIGNTOOL=%ProgramFiles%\Windows Kits\10\bin\x64\signtool.exe"

if not defined SIGNTOOL (
    echo [3/4] [WARN] signtool.exe not found - skipping code signing.
    goto :build_setup
)

if "%SIGN_MODE%"=="thumbprint" goto :sign_thumbprint
echo [3/4] [WARN] SIGN_CERT_PASSWORD is deprecated: the password is visible in process listings.
echo       Import the certificate into the CurrentUser store and set SIGN_CERT_THUMBPRINT instead.
echo [3/4] Signing TimeTracker.exe with the configured certificate...
"%SIGNTOOL%" sign /fd SHA256 /f "%SIGN_CERT_PATH%" /p "%SIGN_CERT_PASSWORD%" "dist\TimeTracker.exe"
if errorlevel 1 goto :sign_failed
:: Pass the same signing tool to Inno Setup so the Setup.exe and the uninstaller
:: are signed too (see the SIGNING_ENABLED block in installer.iss).
set "SIGN_ARGS=/DSIGNING_ENABLED /Sbycert="%SIGNTOOL%" sign /fd SHA256 /f "%SIGN_CERT_PATH%" /p "%SIGN_CERT_PASSWORD%" $f"
set "SIGN_CERT_PASSWORD="
goto :build_setup

:sign_thumbprint
echo [3/4] Signing TimeTracker.exe with the certificate store entry...
"%SIGNTOOL%" sign /fd SHA256 /sha1 "%SIGN_CERT_THUMBPRINT%" "dist\TimeTracker.exe"
if errorlevel 1 goto :sign_failed
set "SIGN_ARGS=/DSIGNING_ENABLED /Sbycert="%SIGNTOOL%" sign /fd SHA256 /sha1 "%SIGN_CERT_THUMBPRINT%" $f"
goto :build_setup

:sign_failed
echo.
echo [ERROR] Signing dist\TimeTracker.exe failed!
echo Verify the certificate configuration and that the certificate is valid.
pause
exit /b 1

:no_signing
echo [3/4] Code signing skipped: set SIGN_CERT_PATH and SIGN_CERT_PASSWORD to enable it.

:build_setup
echo.
echo [4/4] Compiling Setup Installer with Inno Setup...
set "APP_VER="
for /f "delims=" %%v in ('python -c "import sys; sys.path.insert(0, '.'); from time_tracker.core.config import APP_VERSION; print(APP_VERSION)"') do set "APP_VER=%%v"
if not defined APP_VER (
    echo [ERROR] Could not read APP_VERSION from config.py!
    pause
    exit /b 1
)
echo     Version: %APP_VER%
"%ISCC%" %SIGN_ARGS% /DMyAppVersion=%APP_VER% installer.iss
if errorlevel 1 (
    echo.
    echo [ERROR] Inno Setup compilation failed!
    pause
    exit /b 1
)

:: Publish a version.json next to the installer so the local/network update
:: source (update_source_path) can detect and verify the new release offline.
echo.
echo Generating dist\version.json for the local update source...
python -c "import hashlib, json, pathlib, sys; sys.path.insert(0, '.'); from time_tracker.core.config import APP_VERSION; p = pathlib.Path('dist/TimeTracker_Setup.exe'); h = hashlib.sha256(p.read_bytes()).hexdigest(); pathlib.Path('dist/version.json').write_text(json.dumps({'version': APP_VERSION, 'installer': p.name, 'sha256': h, 'notes': ''}, indent=2), encoding='utf-8'); print('version.json ->', APP_VERSION)"
if errorlevel 1 (
    echo [WARN] Could not generate dist\version.json.
)

echo.
echo ========================================================
echo [SUCCESS] Setup Installer built successfully!
echo.
echo Installer file:
echo %~dp0dist\TimeTracker_Setup.exe
if defined SIGN_ARGS echo Signed: yes (signtool)
if not defined SIGN_ARGS echo Signed: no (set SIGN_CERT_PATH and SIGN_CERT_PASSWORD to sign)
echo.
echo You can now send TimeTracker_Setup.exe directly to employees,
echo or copy it with version.json to your local update share.
echo ========================================================
echo.

pause

@echo off
setlocal
title TimeTracker - Upload Release to GitHub

echo ========================================================
echo       TimeTracker - Upload Release to GitHub
echo ========================================================
echo.

cd /d "%~dp0"

python scripts\upload_release.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] Upload failed! Check your internet connection and GitHub token.
    pause
    exit /b 1
)

pause

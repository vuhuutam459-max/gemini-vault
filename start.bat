@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Gemini Vault - launcher

REM ============================================================
REM  Gemini Vault - one-click launcher
REM  Starts the Smart Librarian gateway (FreeLLMAPI) and the
REM  local viewer, then the viewer opens itself in your browser.
REM  Double-click this file to run everything.
REM ============================================================

REM ---- Settings (edit only if you installed things elsewhere) ----
REM  Folder where the FreeLLMAPI gateway was installed.
REM  (On a network share npm symlinks break, so it lives on a local disk.)
set "GATEWAY_DIR=C:\Users\Public\freellmapi"
REM  Port the viewer listens on.
set "VIEWER_PORT=8642"
REM ----------------------------------------------------------------

cd /d "%~dp0"

echo.
echo  ============================================
echo    Gemini Vault - launcher
echo  ============================================
echo.

REM ---- Require Python (the viewer + Smart Librarian core) ----
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] Python was not found in PATH.
    echo      Install Python 3.10+ from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)
echo  [OK] Python found.

REM ---- Start the gateway (optional - the Smart Librarian engine) ----
if exist "%GATEWAY_DIR%\server\dist\index.js" (
    where node >nul 2>&1
    if errorlevel 1 (
        echo  [!] Node.js not found - skipping the gateway.
        echo      Tags / summaries / "Ask the archive" will stay off.
        echo      Install Node 18+ from https://nodejs.org to enable them.
    ) else (
        echo  [OK] Starting FreeLLMAPI gateway  ^( http://localhost:3001 ^)
        start "Gemini Vault - Gateway" /d "%GATEWAY_DIR%" cmd /k node server\dist\index.js
    )
) else (
    echo  [!] Gateway not found at "%GATEWAY_DIR%".
    echo      The viewer still works; Smart Librarian stays disabled until
    echo      you install the gateway. See README ^> Smart Librarian.
)

REM ---- Start the viewer (it opens the browser on its own) ----
echo  [OK] Starting viewer  ^( http://localhost:%VIEWER_PORT% ^)
start "Gemini Vault - Viewer" /d "%~dp0" cmd /k python viewer\serve.py --port %VIEWER_PORT%

echo.
echo  Two windows just opened: Gateway and Viewer.
echo  Your browser will open http://localhost:%VIEWER_PORT% in a moment.
echo  To stop everything, close those two windows.
echo.
echo  You can close THIS window now.
echo.
timeout /t 6 /nobreak >nul
exit /b 0

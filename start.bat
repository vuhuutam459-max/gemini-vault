@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Gemini Vault - launcher

REM ============================================================
REM  Gemini Vault - one-click launcher
REM  Starts the Smart Librarian gateway (FreeLLMAPI) and the
REM  local viewer; the viewer opens itself in your browser.
REM  Double-click this file to run everything.
REM
REM  NOTE: this script works even when the project lives on a
REM  network share (\\server\...). cmd.exe cannot use a UNC path
REM  as the current directory, so we always call python / node
REM  with ABSOLUTE paths instead of relying on the working dir.
REM ============================================================

REM ---- Settings (edit only if you installed things elsewhere) ----
REM  Folder where the FreeLLMAPI gateway was installed.
REM  (On a network share npm symlinks break, so it lives on a local disk.)
set "GATEWAY_DIR=C:\Users\Public\freellmapi"
REM  Port the viewer listens on.
set "VIEWER_PORT=8642"
REM ----------------------------------------------------------------

REM  %~dp0 = folder of THIS .bat (keeps the trailing backslash).
set "HERE=%~dp0"

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
        REM  Gateway lives on a local disk, so /d works fine here.
        start "Gemini Vault - Gateway" /d "%GATEWAY_DIR%" cmd /k node server\dist\index.js
    )
) else (
    echo  [!] Gateway not found at "%GATEWAY_DIR%".
    echo      The viewer still works; Smart Librarian stays disabled until
    echo      you install the gateway. See README ^> Smart Librarian.
)

REM ---- Start the viewer ----
REM  Call python with the ABSOLUTE path to serve.py so it does not matter
REM  that cmd's working dir may be C:\Windows on a UNC share.
echo  [OK] Starting viewer  ^( http://localhost:%VIEWER_PORT% ^)
start "Gemini Vault - Viewer" cmd /k python "%HERE%viewer\serve.py" --port %VIEWER_PORT%

echo.
echo  Two windows just opened: Gateway and Viewer.
echo  Your browser will open http://localhost:%VIEWER_PORT% in a moment.
echo  ^(If it says "page unavailable", wait 3-5 seconds and refresh - the
echo   server needs a moment to start.^)
echo  To stop everything, close those two windows.
echo.
echo  You can close THIS window now.
echo.
timeout /t 6 /nobreak >nul
exit /b 0

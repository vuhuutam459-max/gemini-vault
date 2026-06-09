@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Gemini Vault - launcher

REM ============================================================
REM  Gemini Vault - one-click launcher (anti-fragile edition)
REM
REM  Guarantees the viewer ALWAYS runs from the project's own
REM  virtual environment (.venv), so "pip installed into one
REM  Python, server started from another" can never happen again:
REM    1. .venv missing  -> it is created and dependencies installed.
REM    2. .venv damaged  -> dependencies are re-installed (self-heal).
REM    3. PATH is NEVER trusted to pick the interpreter that runs
REM       the server - only the absolute .venv path is used.
REM
REM  Also starts the Smart Librarian gateway (FreeLLMAPI) if present.
REM
REM  NOTE: works from a network share (\\server\...). cmd.exe cannot
REM  use a UNC path as the current directory, so everything is
REM  called with ABSOLUTE paths instead of relying on the working dir.
REM ============================================================

REM ---- Settings (edit only if you installed things elsewhere) ----
set "GATEWAY_DIR=C:\Users\Public\freellmapi"
set "VIEWER_PORT=8642"
REM ----------------------------------------------------------------

REM  %~dp0 = folder of THIS .bat (keeps the trailing backslash).
set "HERE=%~dp0"
set "VENV_PY=%HERE%.venv\Scripts\python.exe"

echo.
echo  ============================================
echo    Gemini Vault - launcher
echo  ============================================
echo.

REM ---- [1/3] Make sure the project .venv exists ----
if exist "%VENV_PY%" goto venv_ok

echo  [..] First run: creating the project virtual environment...
set "SYS_PY="
REM  Probe known real installs first; a PATH "python" may be the useless
REM  Microsoft Store stub, so it is only a last resort and is test-run.
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
) do (
    if not defined SYS_PY if exist "%%~P" set "SYS_PY=%%~P"
)
if not defined SYS_PY (
    python -c "import sys" >nul 2>&1 && set "SYS_PY=python"
)
if not defined SYS_PY (
    echo  [X] No working Python 3.10+ found on this PC.
    echo      Install it from https://www.python.org/downloads/
    echo      ^(tick "Add python.exe to PATH"^) and run this file again.
    echo.
    pause
    exit /b 1
)
echo  [OK] Using "!SYS_PY!" to create .venv
"!SYS_PY!" -m venv "%HERE%.venv"
if not exist "%VENV_PY%" (
    echo  [X] Failed to create the virtual environment.
    echo.
    pause
    exit /b 1
)

:venv_ok
REM ---- [2/3] Self-heal: scraper deps must import from THIS .venv ----
"%VENV_PY%" -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo  [..] Installing dependencies into .venv ^(one-time, ~1 min^)...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r "%HERE%requirements.txt"
    if errorlevel 1 (
        echo  [X] pip install failed - check the internet connection and rerun.
        echo.
        pause
        exit /b 1
    )
    echo  [..] Installing the Chromium browser for Playwright...
    "%VENV_PY%" -m playwright install chromium
)
echo  [OK] Python environment: %VENV_PY%

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

REM ---- [3/3] Start the viewer from the .venv interpreter ----
echo  [OK] Starting viewer  ^( http://localhost:%VIEWER_PORT% ^)
start "Gemini Vault - Viewer" cmd /k ""%VENV_PY%" "%HERE%viewer\serve.py" --port %VIEWER_PORT%"

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

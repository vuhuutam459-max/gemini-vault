@echo off
chcp 65001 >nul
rem Use pushd (not "cd /d") so this works even when the folder is on a
rem network share: pushd maps a temporary drive letter to a UNC path and
rem makes it the current directory, so relative paths keep working.
pushd "%~dp0"
title Gemini Vault

rem ============================================================
rem  Gemini Vault launcher
rem  - Option 1 (default): view your vault. Needs ZERO extra
rem    dependencies - pure Python standard library.
rem  - Option 2: set up the optional live scraper, which installs
rem    Playwright + BeautifulSoup and downloads Chromium (~170 MB).
rem    Only people who want live scraping ever pay that cost.
rem ============================================================

rem --- Find a Python launcher (python or py) ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)

if not defined PY (
    echo [ERROR] Python was not found.
    echo Install Python 3.10+ from https://python.org
    echo ^(tick "Add Python to PATH" during installation^), then run this file again.
    echo.
    pause
    popd
    exit /b 1
)

:menu
cls
echo ============================================
echo   Gemini Vault
echo ============================================
echo.
echo   [1] Open my vault  (view chats - no install needed)
echo   [2] Set up / update the live scraper  (downloads ~170 MB)
echo   [Q] Quit
echo.
set "choice="
set /p "choice=Choose and press Enter [default 1]: "

if not defined choice goto open
if /i "%choice%"=="1" goto open
if /i "%choice%"=="2" goto setup
if /i "%choice%"=="q" (
    popd
    exit /b 0
)
goto menu

rem ------------------------------------------------------------
:open
cls
echo ============================================
echo   Gemini Vault
echo ============================================
echo.

rem First run: no database yet -> load the bundled demo data
if not exist "gemini_vault.db" (
    echo First run detected - loading the bundled demo data so you have
    echo something to look at right away...
    echo.
    %PY% processor\parse_and_index.py Source_Accounts\demo_export.json
    echo.
    echo Demo data loaded. To import your own chats, use Google Takeout
    echo or the live scraper - see README.md.
    echo ============================================
    echo.
)

echo Starting the local server...
echo The browser will open automatically.
echo.
echo To stop, close this window or press Ctrl+C.
echo ============================================
echo.

%PY% viewer\serve.py

echo.
echo Server stopped.
pause
popd
exit /b 0

rem ------------------------------------------------------------
:setup
cls
echo ============================================
echo   Live scraper setup
echo ============================================
echo.
echo This installs the optional dependencies needed to pull chats
echo directly from gemini.google.com in a browser:
echo   - Python packages from requirements.txt
echo   - the Chromium browser for Playwright ^(~170 MB download^)
echo.
echo You only need this if you are NOT using Google Takeout.
echo.
set "ok="
set /p "ok=Continue with the download and install? [y/N]: "
if /i not "%ok%"=="y" goto menu

echo.
echo [1/2] Installing Python packages...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Package installation failed. Check your internet connection
    echo and that pip is available, then try again.
    echo.
    pause
    goto menu
)

echo.
echo [2/2] Installing the Chromium browser for Playwright...
%PY% -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Chromium installation failed. Check your internet connection
    echo and try again.
    echo.
    pause
    goto menu
)

echo.
echo ============================================
echo   Setup complete.
echo ============================================
echo.
echo You can now scrape your chats, for example:
echo   %PY% processor\scrape_gemini_url.py --list-all --account you@gmail.com
echo.
echo See README.md for all scraper options.
echo.
pause
goto menu

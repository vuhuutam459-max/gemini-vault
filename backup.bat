@echo off
REM ============================================================
REM  Gemini Vault — background chat backup (for Task Scheduler)
REM  Usage:  backup.bat you@gmail.com
REM  SHA256 deduplication is already built into the scraper —
REM  only new/changed chats are fetched.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "ACCOUNT=%~1"

where python >nul 2>nul
if %errorlevel%==0 (
    python processor\scrape_gemini_url.py --list-all --account "%ACCOUNT%" --log .scraper_log.txt
    goto end
)

where py >nul 2>nul
if %errorlevel%==0 (
    py processor\scrape_gemini_url.py --list-all --account "%ACCOUNT%" --log .scraper_log.txt
    goto end
)

echo [ERROR] Python not found in PATH.

:end

@echo off
setlocal

cd /d "%~dp0"

if not exist "%~dp0seda\magalu\log" mkdir "%~dp0seda\magalu\log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "SEDA_RUN_TIMESTAMP=%%i"
if not defined SEDA_RUN_LOG_FILE set "SEDA_RUN_LOG_FILE=%~dp0seda\magalu\log\magalu_tv_smoke_%SEDA_RUN_TIMESTAMP%.log"
if not defined SEDA_MAGALU_BROWSER_PROFILE set "SEDA_MAGALU_BROWSER_PROFILE=C:\tmp\seda_magalu_profile_%SEDA_RUN_TIMESTAMP%"

set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

set SEDA_PRODUCT_LINE=TV
set SEDA_RETAILERS=magalu
set SEDA_MAIN_PAGE_LIST=1
set SEDA_BSR_PAGE_LIST=1
set SEDA_DETAIL_LIMIT=3
set SEDA_DETAIL_SKIP=
set SEDA_RUN_ROOT=%~dp0seda\data\magalu\tv_smoke_%SEDA_RUN_TIMESTAMP%

set SEDA_EMAIL_NOTIFY=0
set SEDA_EMAIL_DRY_RUN=1
set SEDA_DB_INSERT=0
set SEDA_ALLOW_ZENROWS=0

set SEDA_POSTAL_CODE=01001-001
set SEDA_MAGALU_LISTING_FETCH_MODE=browser
set SEDA_MAGALU_HTML_BROWSER_FALLBACK=1
set SEDA_MAGALU_REVIEW_HTML_MAX_PAGES=4
set SEDA_MAGALU_SHIPPING_BLANK_RETRY_LIMIT=0

call :log "[SEDA] Magalu TV smoke run started"
call :log "[SEDA] log file: %SEDA_RUN_LOG_FILE%"
call :log "[SEDA] run root: %SEDA_RUN_ROOT%"
call :log "[SEDA] browser profile: %SEDA_MAGALU_BROWSER_PROFILE%"

call python -m seda.magalu.magalu_orchestrator main_list main_targets bsr_list bsr_rank final_targets detail_enrichment review20 final_output field_audit
if errorlevel 1 goto :failed

call :log "[SEDA] Magalu TV smoke run completed"
exit /b 0

:log
echo %~1
>> "%SEDA_RUN_LOG_FILE%" echo %~1
exit /b 0

:failed
call :log "[SEDA] Magalu TV smoke run failed"
exit /b 1

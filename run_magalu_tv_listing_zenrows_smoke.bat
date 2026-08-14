@echo off
setlocal

cd /d "%~dp0"

if not exist "%~dp0seda\magalu\log" mkdir "%~dp0seda\magalu\log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "SEDA_RUN_TIMESTAMP=%%i"

set "SEDA_RUN_ROOT=%~dp0seda\data\magalu\listing_zenrows_smoke\tv\%SEDA_RUN_TIMESTAMP%"
set "SEDA_RUN_LOG_FILE=%~dp0seda\magalu\log\magalu_tv_listing_zenrows_smoke_%SEDA_RUN_TIMESTAMP%.log"
set "SEDA_FORCE_DATED_RUN_ROOT=0"
set "SEDA_REUSE_RAW=0"

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "SEDA_PRODUCT_LINE=TV"
set "SEDA_RETAILERS=magalu"
set "SEDA_RUN_ID=main"
set "SEDA_MAIN_PAGE_LIST=1"
set "SEDA_MAIN_UNIQUE_TARGET=0"
set "SEDA_ALLOW_EMPTY_LISTING=0"
set "SEDA_MAGALU_MAIN_URL_TV=https://www.magazineluiza.com.br/busca/tv/"

rem Force only the centralized ZenRows listing ladder for this one-page test.
set "SEDA_MAGALU_LISTING_FETCH_MODE=zenrows"
set "SEDA_MAGALU_LISTING_ALLOW_ZENROWS=1"
set "SEDA_MAGALU_LISTING_ZENROWS_DRY_RUN=0"
set "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_FIRST=1"
set "SEDA_MAGALU_LISTING_ZENROWS_GRAPHQL_PROFILE=premium_html"
set "SEDA_MAGALU_LISTING_ZENROWS_PROFILE=premium_html"
set "SEDA_MAGALU_LISTING_ZENROWS_TIMEOUT=45"
set "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_PROFILES=listing_next_data_js_wait"
set "SEDA_MAGALU_LISTING_ZENROWS_FALLBACK_SLEEP_SECONDS=2"
set "SEDA_MAGALU_LISTING_FAIL_FAST=1"
set "SEDA_MAGALU_SEARCH_PAGE_SIZE=60"
set "SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES="
set "SEDA_ALLOW_ZENROWS=1"
set "SEDA_ZENROWS_DRY_RUN=0"
set "SEDA_ZENROWS_PROXY_COUNTRY=br"
set "SEDA_ZENROWS_CUSTOM_HEADERS=1"

rem Defense in depth. The only selected orchestrator step is main_list.
set "SEDA_DB_INSERT=0"
set "SEDA_EMAIL_NOTIFY=0"
set "SEDA_EMAIL_DRY_RUN=1"

call :log "[SEDA] Magalu TV listing ZenRows smoke started"
call :log "[SEDA] run root: %SEDA_RUN_ROOT%"
call :log "[SEDA] log file: %SEDA_RUN_LOG_FILE%"

call python -m seda.magalu.magalu_orchestrator --product-line TV main_list
set "SEDA_CRAWL_EXIT=%ERRORLEVEL%"

call python -m seda.magalu.listing_smoke_check --run-root "%SEDA_RUN_ROOT%"
set "SEDA_CHECK_EXIT=%ERRORLEVEL%"

call :log "[SEDA] crawl exit: %SEDA_CRAWL_EXIT%"
call :log "[SEDA] check exit: %SEDA_CHECK_EXIT%"

if not "%SEDA_CRAWL_EXIT%"=="0" goto :failed
if not "%SEDA_CHECK_EXIT%"=="0" goto :failed

call :log "[SEDA] Magalu TV listing ZenRows smoke passed"
exit /b 0

:log
echo %~1
>> "%SEDA_RUN_LOG_FILE%" echo %~1
exit /b 0

:failed
call :log "[SEDA] Magalu TV listing ZenRows smoke failed"
exit /b 1

@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

if not defined SEDA_MAGALU_BROWSER_BASE_TIMEOUT set SEDA_MAGALU_BROWSER_BASE_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT set SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT set SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT set SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_ATTEMPTS set SEDA_MAGALU_BROWSER_ATTEMPTS=1
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS set SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS=1
if not defined SEDA_MAGALU_PROBE_READY_TIMEOUT set SEDA_MAGALU_PROBE_READY_TIMEOUT=20
if not defined SEDA_MAGALU_PROBE_SETTLE_SECONDS set SEDA_MAGALU_PROBE_SETTLE_SECONDS=1

if "%~1"=="" (
    set "PRODUCT_LINE=TV"
    set "PAGES=1,2,3"
    goto args_done
)
set "PRODUCT_LINE=%~1"
shift
set "PAGES="

:collect_pages
if "%~1"=="" goto pages_done
if defined PAGES (
    set "PAGES=!PAGES!,%~1"
) else (
    set "PAGES=%~1"
)
shift
goto collect_pages

:pages_done
if not defined PAGES set "PAGES=1,2,3"

:args_done

echo [SEDA] Magalu listing GraphQL compare probe product=%PRODUCT_LINE% pages=%PAGES%
python -m seda.magalu.probe_listing_graphql_compare --product-line "%PRODUCT_LINE%" --run-id main --pages "%PAGES%" --timeout 8 --browser-ready-timeout %SEDA_MAGALU_PROBE_READY_TIMEOUT% --browser-settle-seconds %SEDA_MAGALU_PROBE_SETTLE_SECONDS%
exit /b %ERRORLEVEL%

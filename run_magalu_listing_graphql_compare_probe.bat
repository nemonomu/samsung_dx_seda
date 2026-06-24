@echo off
setlocal

cd /d "%~dp0"

if not defined SEDA_MAGALU_BROWSER_BASE_TIMEOUT set SEDA_MAGALU_BROWSER_BASE_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT set SEDA_MAGALU_BROWSER_PAGE_LOAD_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT set SEDA_MAGALU_BROWSER_SCRIPT_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT set SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT=8
if not defined SEDA_MAGALU_BROWSER_ATTEMPTS set SEDA_MAGALU_BROWSER_ATTEMPTS=1
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS set SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS=1

if "%~1"=="" (
    set "PRODUCT_LINE=TV"
) else (
    set "PRODUCT_LINE=%~1"
)

if "%~2"=="" (
    set "PAGES=1,2,3"
) else (
    set "PAGES=%~2"
)

echo [SEDA] Magalu listing GraphQL compare probe product=%PRODUCT_LINE% pages=%PAGES%
python -m seda.magalu.probe_listing_graphql_compare --product-line %PRODUCT_LINE% --run-id main --pages %PAGES% --timeout 8 --browser-warmup-seconds 1
exit /b %ERRORLEVEL%

@echo off
setlocal

cd /d "%~dp0"

rem Shared defaults for stable RDP full runs.
if not defined SEDA_POSTAL_CODE set SEDA_POSTAL_CODE=01001-001
if not defined SEDA_TIMEOUT set SEDA_TIMEOUT=25
if not defined SEDA_RETAILER_SWITCH_SLEEP_SECONDS set SEDA_RETAILER_SWITCH_SLEEP_SECONDS=0
set SEDA_RUN_ROOT=

rem Magalu defaults.
if not defined SEDA_FETCH_MODE set SEDA_FETCH_MODE=magalu_graphql_first
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS set SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS=1
if not defined SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS set SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS=1
if not defined SEDA_MAGALU_SEARCH_RETRIES set SEDA_MAGALU_SEARCH_RETRIES=0
if not defined SEDA_MAGALU_DETAIL_RETRIES set SEDA_MAGALU_DETAIL_RETRIES=0
if not defined SEDA_MAGALU_REVIEW_RETRIES set SEDA_MAGALU_REVIEW_RETRIES=0
if not defined SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS set SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS=0
if not defined SEDA_MAGALU_REVIEW_SLEEP_SECONDS set SEDA_MAGALU_REVIEW_SLEEP_SECONDS=0
if not defined SEDA_MAGALU_REVIEW_HTML_MAX_PAGES set SEDA_MAGALU_REVIEW_HTML_MAX_PAGES=10
if not defined SEDA_MAGALU_DETAIL_HTML_FALLBACK set SEDA_MAGALU_DETAIL_HTML_FALLBACK=0
if not defined SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD set SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD=5
if not defined SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD set SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD=5
if not defined SEDA_MAGALU_BROWSER_HTML_ATTEMPTS set SEDA_MAGALU_BROWSER_HTML_ATTEMPTS=1
if not defined SEDA_MAGALU_PDP_NAV_FALLBACK set SEDA_MAGALU_PDP_NAV_FALLBACK=0
if not defined SEDA_MAGALU_BROWSER_MAX_USES set SEDA_MAGALU_BROWSER_MAX_USES=50
if not defined SEDA_MAGALU_BROWSER_MAX_AGE_SECONDS set SEDA_MAGALU_BROWSER_MAX_AGE_SECONDS=1200
if not defined SEDA_MAGALU_BROWSER_RESTART_SLEEP_SECONDS set SEDA_MAGALU_BROWSER_RESTART_SLEEP_SECONDS=2
if not defined SEDA_MAGALU_BROWSER_CLOSE_ON_EXIT set SEDA_MAGALU_BROWSER_CLOSE_ON_EXIT=1
set SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES=

rem Casas Bahia CDP defaults.
if not defined SEDA_CDP_CLOSE_EXISTING_TABS set SEDA_CDP_CLOSE_EXISTING_TABS=1
if not defined SEDA_CDP_USER_DATA_DIR set SEDA_CDP_USER_DATA_DIR=C:\tmp\seda_casas_bahia_cdp_profile

call :run_stage Magalu TV seda.magalu.magalu_orchestrator TV
if errorlevel 1 exit /b 1
call :sleep_between

call :run_stage "Casas Bahia" TV seda.casas_bahia.casas_bahia_orchestrator TV
if errorlevel 1 exit /b 1
call :sleep_between

call :run_stage Magalu REF seda.magalu.magalu_orchestrator REF
if errorlevel 1 exit /b 1
call :sleep_between

call :run_stage "Casas Bahia" REF seda.casas_bahia.casas_bahia_orchestrator REF
if errorlevel 1 exit /b 1
call :sleep_between

call :run_stage Magalu LDY seda.magalu.magalu_orchestrator LDY
if errorlevel 1 exit /b 1
call :sleep_between

call :run_stage "Casas Bahia" LDY seda.casas_bahia.casas_bahia_orchestrator LDY
if errorlevel 1 exit /b 1

echo [SEDA] Magalu/Casas Bahia interleaved TV/REF/LDY full run completed
exit /b 0

:run_stage
set "RETAILER_LABEL=%~1"
set "PRODUCT_LABEL=%~2"
set "MODULE_NAME=%~3"
set "PRODUCT_LINE=%~4"
echo [SEDA] %RETAILER_LABEL% %PRODUCT_LABEL% full run started
call python -m %MODULE_NAME% --product-line %PRODUCT_LINE% --all
if errorlevel 1 (
    echo [SEDA] %RETAILER_LABEL% %PRODUCT_LABEL% full run failed
    exit /b 1
)
echo [SEDA] %RETAILER_LABEL% %PRODUCT_LABEL% full run completed
exit /b 0

:sleep_between
if "%SEDA_RETAILER_SWITCH_SLEEP_SECONDS%"=="0" exit /b 0
echo [SEDA] waiting %SEDA_RETAILER_SWITCH_SLEEP_SECONDS% seconds before next retailer/product line
timeout /t %SEDA_RETAILER_SWITCH_SLEEP_SECONDS% /nobreak >nul
exit /b 0

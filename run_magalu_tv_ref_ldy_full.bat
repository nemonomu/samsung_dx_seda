@echo off
setlocal

cd /d "%~dp0"

if not defined SEDA_POSTAL_CODE set SEDA_POSTAL_CODE=01001-001
if not defined SEDA_TIMEOUT set SEDA_TIMEOUT=25
if not defined SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS set SEDA_MAGALU_BROWSER_GRAPHQL_ATTEMPTS=1
if not defined SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS set SEDA_MAGALU_SEARCH_BROWSER_ATTEMPTS=1
if not defined SEDA_MAGALU_SEARCH_RETRIES set SEDA_MAGALU_SEARCH_RETRIES=0
if not defined SEDA_MAGALU_DETAIL_RETRIES set SEDA_MAGALU_DETAIL_RETRIES=0
if not defined SEDA_MAGALU_REVIEW_RETRIES set SEDA_MAGALU_REVIEW_RETRIES=0
if not defined SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS set SEDA_MAGALU_REVIEW_INITIAL_SLEEP_SECONDS=0
if not defined SEDA_MAGALU_REVIEW_SLEEP_SECONDS set SEDA_MAGALU_REVIEW_SLEEP_SECONDS=0
if not defined SEDA_MAGALU_DETAIL_HTML_FALLBACK set SEDA_MAGALU_DETAIL_HTML_FALLBACK=0
if not defined SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD set SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD=5
if not defined SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD set SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD=5
if not defined SEDA_MAGALU_BROWSER_HTML_ATTEMPTS set SEDA_MAGALU_BROWSER_HTML_ATTEMPTS=1
if not defined SEDA_MAGALU_PDP_NAV_FALLBACK set SEDA_MAGALU_PDP_NAV_FALLBACK=0
set SEDA_MAGALU_SEARCH_FALLBACK_PAGE_SIZES=

echo [SEDA] Magalu TV full run started
call python -m seda.magalu.magalu_orchestrator --product-line TV --all
if errorlevel 1 goto :failed_tv

echo [SEDA] Magalu REF full run started
call python -m seda.magalu.magalu_orchestrator --product-line REF --all
if errorlevel 1 goto :failed_ref

echo [SEDA] Magalu LDY full run started
call python -m seda.magalu.magalu_orchestrator --product-line LDY --all
if errorlevel 1 goto :failed_ldy

echo [SEDA] Magalu TV/REF/LDY full run completed
exit /b 0

:failed_tv
echo [SEDA] Magalu TV full run failed
exit /b 1

:failed_ref
echo [SEDA] Magalu REF full run failed
exit /b 1

:failed_ldy
echo [SEDA] Magalu LDY full run failed
exit /b 1

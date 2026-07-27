@echo off
REM Resume Magalu TV step08 after a browser crash, then continue the pipeline.
REM Usage: resume_magalu_tv_step08.bat <skip_rows> [YYYYMMDD]
REM   skip_rows = data-row count already in final_output_enriched.csv (checkpointed).
setlocal
if "%~1"=="" (echo Usage: %~nx0 ^<skip_rows^> [YYYYMMDD] & exit /b 1)
if "%~2"=="" (for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set RUNDATE=%%d) else set RUNDATE=%~2
cd /d "%~dp0"

set SEDA_RUN_ROOT=%~dp0seda\data\magalu\tv\%RUNDATE%
set SEDA_PRODUCT_LINE=TV
set SEDA_POSTAL_CODE=01001-001
set SEDA_TIMEOUT=25
set SEDA_MAGALU_DETAIL_GRAPHQL=1
set SEDA_MAGALU_BROWSER_GRAPHQL=1
set SEDA_MAGALU_REVIEW_GRAPHQL=1
set SEDA_MAGALU_HTML_REQUESTS_FETCH=1
set SEDA_MAGALU_HTML_BROWSER_FALLBACK=0
set SEDA_MAGALU_REVIEW_HTML_MAX_PAGES=10
set SEDA_MAGALU_SHIPPING_BLANK_RETRY=0
if not defined SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK set SEDA_MAGALU_LAST_KNOWN_DB_FALLBACK=1
if not defined SEDA_MAGALU_LAST_KNOWN_HISTORY_LIMIT set SEDA_MAGALU_LAST_KNOWN_HISTORY_LIMIT=30
if not defined SEDA_MAGALU_LAST_KNOWN_DB_TIMEOUT_MS set SEDA_MAGALU_LAST_KNOWN_DB_TIMEOUT_MS=15000

REM resume-specific: skip done rows, disable GraphQL page reuse, recycle browser sooner
set SEDA_DETAIL_SKIP=%~1
set SEDA_MAGALU_GRAPHQL_REUSE_PAGE=0
set SEDA_MAGALU_BROWSER_MAX_USES=30

echo [resume] run_root=%SEDA_RUN_ROOT% skip=%SEDA_DETAIL_SKIP%
python -m seda.magalu.step08_detail_enrichment || exit /b 1
python -m seda.magalu.magalu_orchestrator --from-step review20 --product-line TV
endlocal

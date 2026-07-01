@echo off
setlocal
REM Resume Magalu step08 after a crash, then continue the pipeline.
REM Usage: resume_magalu_step08.bat <TV|REF|LDY> <skip_rows> [YYYYMMDD]
REM   skip_rows = data-row count already in final_output_enriched.csv (checkpointed, multiple of 25).
if "%~2"=="" (echo Usage: %~nx0 ^<TV^|REF^|LDY^> ^<skip_rows^> [YYYYMMDD] & exit /b 1)
set SEDA_LINE=%~1
if /i "%SEDA_LINE%"=="TV" set LINE_DIR=tv
if /i "%SEDA_LINE%"=="REF" set LINE_DIR=ref
if /i "%SEDA_LINE%"=="LDY" set LINE_DIR=ldy
if not defined LINE_DIR (echo Unknown line "%SEDA_LINE%" ^(use TV^|REF^|LDY^) & exit /b 1)
if "%~3"=="" (for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set RUNDATE=%%d) else set RUNDATE=%~3
cd /d "%~dp0"

set SEDA_RUN_ROOT=%~dp0seda\data\magalu\%LINE_DIR%\%RUNDATE%
set SEDA_PRODUCT_LINE=%SEDA_LINE%
set SEDA_POSTAL_CODE=01001-001
set SEDA_TIMEOUT=25
set SEDA_MAGALU_DETAIL_GRAPHQL=1
set SEDA_MAGALU_BROWSER_GRAPHQL=1
set SEDA_MAGALU_REVIEW_GRAPHQL=1
set SEDA_MAGALU_HTML_REQUESTS_FETCH=1
set SEDA_MAGALU_HTML_BROWSER_FALLBACK=0
set SEDA_MAGALU_REVIEW_HTML_MAX_PAGES=10
set SEDA_MAGALU_SHIPPING_BLANK_RETRY=0

REM anti-crash: keep GraphQL page-reuse ON (fewer navigations), disable browser
REM recycling (recycle -> navigation -> DrissionPage crash), fast graphql recovery.
set SEDA_MAGALU_BROWSER_MAX_USES=0
set SEDA_MAGALU_BROWSER_MAX_AGE_SECONDS=3600
set SEDA_MAGALU_BROWSER_GRAPHQL_TIMEOUT=25
set SEDA_DETAIL_SKIP=%~2

echo [resume] run_root=%SEDA_RUN_ROOT% line=%SEDA_LINE% skip=%SEDA_DETAIL_SKIP%
python -m seda.magalu.step08_detail_enrichment || exit /b 1
python -m seda.magalu.magalu_orchestrator --from-step review20 --product-line %SEDA_LINE%
endlocal

@echo off
setlocal

cd /d "%~dp0"

rem Listing: 1=REST API, 1-1=URL-first REST API, 2=hybrid, 3=UC + API, 4=URL-first UC + API.
rem Final selected default. Optional first argument 1/1-1/2/3/4 overrides this run only.
set "SEDA_CASAS_BAHIA_LISTING_MODE=1"
if not "%~1"=="" set "SEDA_CASAS_BAHIA_LISTING_MODE=%~1"
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="1" goto :listing_mode_valid
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="1-1" goto :listing_mode_valid
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="2" goto :listing_mode_valid
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="3" goto :listing_mode_valid
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="4" goto :listing_mode_valid
goto :invalid_listing_mode

:listing_mode_valid
rem Optional resume applies only to TV; REF and LDY still run in full.
set "SEDA_CASAS_BAHIA_RESUME_TV=0"
if "%~2"=="" goto :resume_args_valid
if /i not "%~2"=="--resume-tv-detail" goto :invalid_resume
if not "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="4" goto :invalid_resume
if not "%~5"=="" goto :invalid_resume
set "SEDA_CASAS_BAHIA_RESUME_DATE=%~3"
set "SEDA_CASAS_BAHIA_RESUME_COUNT=%~4"
powershell -NoProfile -Command "try { $null=[datetime]::ParseExact($env:SEDA_CASAS_BAHIA_RESUME_DATE,'yyyyMMdd',[System.Globalization.CultureInfo]::InvariantCulture); if ($env:SEDA_CASAS_BAHIA_RESUME_COUNT -notmatch '^[1-9][0-9]*$') { exit 2 }; exit 0 } catch { exit 2 }"
if errorlevel 1 goto :invalid_resume
set "SEDA_CASAS_BAHIA_RESUME_TV=1"
set "SEDA_RUN_DATE=%SEDA_CASAS_BAHIA_RESUME_DATE%"
set "SEDA_FORCE_DATED_RUN_ROOT=1"
rem Do not let inherited TV-only settings leak into REF or LDY.
set "SEDA_DETAIL_SKIP=0"
set "SEDA_DETAIL_LIMIT=0"
set "SEDA_DETAIL_WORKER_ID="
set "SEDA_DETAIL_TRACE_TAG="
set "SEDA_DETAIL_TARGET_CSV="
set "SEDA_DETAIL_OUTPUT_CSV="
set "SEDA_MAGALU_SHIPPING_BACKFILL_ONLY=0"

:resume_args_valid
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="1" set "SEDA_CASAS_BAHIA_LISTING_MODE_LABEL=rest_api"
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="1-1" set "SEDA_CASAS_BAHIA_LISTING_MODE_LABEL=rest_api_url_first"
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="2" set "SEDA_CASAS_BAHIA_LISTING_MODE_LABEL=hybrid"
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="3" set "SEDA_CASAS_BAHIA_LISTING_MODE_LABEL=uc_api"
if "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="4" set "SEDA_CASAS_BAHIA_LISTING_MODE_LABEL=uc_api_url_first"

if not exist "%~dp0seda\casas_bahia\log" mkdir "%~dp0seda\casas_bahia\log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "SEDA_RUN_TIMESTAMP=%%i"
if not defined SEDA_RUN_LOG_FILE set "SEDA_RUN_LOG_FILE=%~dp0seda\casas_bahia\log\casas_bahia_tv_ref_ldy_full_%SEDA_RUN_TIMESTAMP%.log"
if not defined PYTHONUNBUFFERED set PYTHONUNBUFFERED=1
if not defined PYTHONIOENCODING set PYTHONIOENCODING=utf-8
rem Restored Mode 1 keeps the pre-20260922 environment retry setting (default 2).
rem Other modes keep their existing ceiling: 1 initial call + 2 retries.
if not "%SEDA_CASAS_BAHIA_LISTING_MODE%"=="1" set SEDA_CASAS_BAHIA_SEARCH_RETRIES=2
if not defined SEDA_CASAS_BAHIA_DEFAULT_ALLOW_ZENROWS set SEDA_CASAS_BAHIA_DEFAULT_ALLOW_ZENROWS=1
if not defined SEDA_CASAS_BAHIA_DEFAULT_ZENROWS_DRY_RUN set SEDA_CASAS_BAHIA_DEFAULT_ZENROWS_DRY_RUN=0
if not defined SEDA_CASAS_BAHIA_PRODUCT_SOURCE_ZENROWS_RETRIES set SEDA_CASAS_BAHIA_PRODUCT_SOURCE_ZENROWS_RETRIES=0
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK set SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK=1
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_10X set SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_10X=premium_html
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_25X set SEDA_CASAS_BAHIA_ZENROWS_FIELD_PROFILE_25X=pdp_js_full
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_TIMEOUT set SEDA_CASAS_BAHIA_ZENROWS_FIELD_TIMEOUT=45
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK set SEDA_CASAS_BAHIA_ZENROWS_FIELD_FAILURE_STREAK=3
if not defined SEDA_CASAS_BAHIA_ZENROWS_FIELD_CHECKPOINT_EVERY set SEDA_CASAS_BAHIA_ZENROWS_FIELD_CHECKPOINT_EVERY=5
if not defined SEDA_CASAS_BAHIA_LAST_KNOWN_DB_FALLBACK set SEDA_CASAS_BAHIA_LAST_KNOWN_DB_FALLBACK=1
if not defined SEDA_CASAS_BAHIA_LAST_KNOWN_HISTORY_LIMIT set SEDA_CASAS_BAHIA_LAST_KNOWN_HISTORY_LIMIT=30
if not defined SEDA_CASAS_BAHIA_LAST_KNOWN_DB_TIMEOUT_MS set SEDA_CASAS_BAHIA_LAST_KNOWN_DB_TIMEOUT_MS=15000

if not defined SEDA_POSTAL_CODE set SEDA_POSTAL_CODE=01001-001
set SEDA_FETCH_MODE=graphql
set SEDA_RETRY_SLEEP_SECONDS=0

call :log "[SEDA] log file: %SEDA_RUN_LOG_FILE%"
call :log "[SEDA] Casas Bahia listing mode=%SEDA_CASAS_BAHIA_LISTING_MODE% (%SEDA_CASAS_BAHIA_LISTING_MODE_LABEL%)"
if "%SEDA_CASAS_BAHIA_RESUME_TV%"=="1" goto :resume_tv
call :log "[SEDA] Casas Bahia TV full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line TV --all
if errorlevel 1 goto :failed_tv
goto :start_ref

:resume_tv
call :log "[SEDA] Casas Bahia TV resume date=%SEDA_RUN_DATE% completed_detail_rows=%SEDA_CASAS_BAHIA_RESUME_COUNT%"
call :run_tv_detail_resume
if errorlevel 1 goto :failed_tv

:start_ref
call :log "[SEDA] Casas Bahia REF full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line REF --all
if errorlevel 1 goto :failed_ref

call :log "[SEDA] Casas Bahia LDY full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line LDY --all
if errorlevel 1 goto :failed_ldy

call :log "[SEDA] Casas Bahia TV/REF/LDY full run completed"
exit /b 0

:run_tv_detail_resume
setlocal
set "SEDA_DETAIL_SKIP=%SEDA_CASAS_BAHIA_RESUME_COUNT%"
set "SEDA_DETAIL_TARGET_CSV=%~dp0seda\data\casas_bahia\tv\%SEDA_RUN_DATE%\output\seda_final_targets.csv"
set "SEDA_DETAIL_OUTPUT_CSV=%~dp0seda\data\casas_bahia\tv\%SEDA_RUN_DATE%\output\final_output_enriched.csv"
set "SEDA_CASAS_BAHIA_API_ENRICH=1"
set "SEDA_CASAS_BAHIA_ZENROWS_FIELD_FALLBACK=0"
set "SEDA_DETAIL_TRACE=1"
set "SEDA_DETAIL_TRACE_TAG="
if not exist "%SEDA_DETAIL_TARGET_CSV%" (
    call :log "[SEDA] TV resume failed: original target CSV missing"
    endlocal & exit /b 1
)
if not exist "%SEDA_DETAIL_OUTPUT_CSV%" (
    call :log "[SEDA] TV resume failed: original enriched CSV missing"
    endlocal & exit /b 1
)
if not exist "%~dp0seda\data\casas_bahia\tv\%SEDA_RUN_DATE%\detail\trace\subcall_trace.csv" (
    call :log "[SEDA] TV resume failed: original subcall trace missing"
    endlocal & exit /b 1
)
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line TV --from-step detail_enrichment --skip-local-cleanup
set "SEDA_CASAS_BAHIA_RESUME_EXIT=%errorlevel%"
endlocal & exit /b %SEDA_CASAS_BAHIA_RESUME_EXIT%

:log
echo %~1
>> "%SEDA_RUN_LOG_FILE%" echo %~1
exit /b 0

:failed_tv
call :log "[SEDA] Casas Bahia TV full run failed"
exit /b 1

:failed_ref
call :log "[SEDA] Casas Bahia REF full run failed"
exit /b 1

:failed_ldy
call :log "[SEDA] Casas Bahia LDY full run failed"
exit /b 1

:invalid_listing_mode
echo [SEDA] Invalid listing mode. Use 1=REST API, 1-1=URL-first REST API, 2=hybrid, 3=UC+API, or 4=URL-first UC+API.
exit /b 2

:invalid_resume
echo [SEDA] Resume usage: run_casas_bahia_tv_ref_ldy_full.bat 4 --resume-tv-detail YYYYMMDD completed_detail_rows
exit /b 2

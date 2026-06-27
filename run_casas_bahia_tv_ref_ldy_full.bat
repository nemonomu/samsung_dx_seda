@echo off
setlocal

cd /d "%~dp0"

if not exist "%~dp0seda\casas_bahia\log" mkdir "%~dp0seda\casas_bahia\log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "SEDA_RUN_TIMESTAMP=%%i"
if not defined SEDA_RUN_LOG_FILE set "SEDA_RUN_LOG_FILE=%~dp0seda\casas_bahia\log\casas_bahia_tv_ref_ldy_full_%SEDA_RUN_TIMESTAMP%.log"
if not defined PYTHONUNBUFFERED set PYTHONUNBUFFERED=1
if not defined PYTHONIOENCODING set PYTHONIOENCODING=utf-8

if not defined SEDA_POSTAL_CODE set SEDA_POSTAL_CODE=01001-001
set SEDA_FETCH_MODE=graphql
set SEDA_RETRY_SLEEP_SECONDS=0

call :log "[SEDA] log file: %SEDA_RUN_LOG_FILE%"
call :log "[SEDA] Casas Bahia TV full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line TV --all
if errorlevel 1 goto :failed_tv

call :log "[SEDA] Casas Bahia REF full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line REF --all
if errorlevel 1 goto :failed_ref

call :log "[SEDA] Casas Bahia LDY full run started"
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line LDY --all
if errorlevel 1 goto :failed_ldy

call :log "[SEDA] Casas Bahia TV/REF/LDY full run completed"
exit /b 0

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

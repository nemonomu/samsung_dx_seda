@echo off
setlocal

cd /d "%~dp0"

echo [SEDA] Casas Bahia REF full run started
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line REF --all
if errorlevel 1 goto :failed

echo [SEDA] Casas Bahia REF full run completed
exit /b 0

:failed
echo [SEDA] Casas Bahia REF full run failed
exit /b 1

@echo off
setlocal

cd /d "%~dp0"

echo [SEDA] Casas Bahia TV full run started
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line TV --all
if errorlevel 1 goto :failed

echo [SEDA] Casas Bahia TV full run completed
exit /b 0

:failed
echo [SEDA] Casas Bahia TV full run failed
exit /b 1

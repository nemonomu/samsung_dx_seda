@echo off
setlocal

cd /d "%~dp0"

echo [SEDA] Casas Bahia TV full run started
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line TV --all
if errorlevel 1 goto :failed_tv

echo [SEDA] Casas Bahia REF full run started
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line REF --all
if errorlevel 1 goto :failed_ref

echo [SEDA] Casas Bahia LDY full run started
call python -m seda.casas_bahia.casas_bahia_orchestrator --product-line LDY --all
if errorlevel 1 goto :failed_ldy

echo [SEDA] Casas Bahia TV/REF/LDY full run completed
exit /b 0

:failed_tv
echo [SEDA] Casas Bahia TV full run failed
exit /b 1

:failed_ref
echo [SEDA] Casas Bahia REF full run failed
exit /b 1

:failed_ldy
echo [SEDA] Casas Bahia LDY full run failed
exit /b 1

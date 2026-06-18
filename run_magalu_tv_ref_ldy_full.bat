@echo off
setlocal

cd /d "%~dp0"

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

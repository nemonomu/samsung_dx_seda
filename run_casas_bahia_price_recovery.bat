@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
python -u -B -m seda.casas_bahia.price_recovery %*
set "RECOVERY_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RECOVERY_EXIT%

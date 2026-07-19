@echo off
setlocal
cd /d "%~dp0"
REM Sequential full runs: Magalu REF -> Casas REF -> Magalu LDY -> Casas LDY.
REM Each step is isolated (child bats use their own setlocal); a failure is logged
REM and the sequence continues so the other categories still get collected.
set "FAILED="
set "SEDA_RUN_ROOT="
set "SEDA_COMBINED_RETAILER_RUN=1"
set "SEDA_DB_REPLACE_RETAILER_BEFORE_LOAD="
set "SEDA_FORCE_DATED_RUN_ROOT=1"

echo ===== [1/4] Magalu REF =====
call "%~dp0run_magalu_ref_full.bat"
if errorlevel 1 set "FAILED=%FAILED% magalu-ref"

echo ===== [2/4] Casas REF =====
call "%~dp0run_casas_bahia_ref_full.bat"
if errorlevel 1 set "FAILED=%FAILED% casas-ref"

echo ===== [3/4] Magalu LDY =====
call "%~dp0run_magalu_ldy_full.bat"
if errorlevel 1 set "FAILED=%FAILED% magalu-ldy"

echo ===== [4/4] Casas LDY =====
call "%~dp0run_casas_bahia_ldy_full.bat"
if errorlevel 1 set "FAILED=%FAILED% casas-ldy"

echo ===== sequence done =====
if defined FAILED (
  echo FAILED:%FAILED%
  exit /b 1
)
echo all 4 completed
endlocal

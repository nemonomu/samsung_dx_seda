@echo off
REM Magalu LDY full run. Thin wrapper over run_magalu_full.bat LDY.
call "%~dp0run_magalu_full.bat" LDY
exit /b %errorlevel%

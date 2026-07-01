@echo off
REM Magalu REF full run. Thin wrapper over run_magalu_full.bat REF.
call "%~dp0run_magalu_full.bat" REF
exit /b %errorlevel%

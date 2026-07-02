@echo off
REM Magalu TV full run. Thin wrapper over run_magalu_full.bat TV.
call "%~dp0run_magalu_full.bat" TV
exit /b %errorlevel%

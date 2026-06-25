@echo off
setlocal

cd /d "%~dp0"

set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

python -m seda.magalu.probe_browser_context_graphql %*

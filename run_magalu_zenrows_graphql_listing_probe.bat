@echo off
setlocal
cd /d C:\samsung_dx_seda
set SEDA_RETAILERS=magalu
set SEDA_PRODUCT_LINE=TV
set SEDA_ALLOW_ZENROWS=1
set SEDA_MAGALU_ZENROWS_GRAPHQL_PAGES=1
set SEDA_MAGALU_ZENROWS_GRAPHQL_RUN_IDS=main,bsr
echo [SEDA] Magalu ZenRows GraphQL listing probe started
python -m seda.magalu.probe_zenrows_graphql_listing TV --execute
if errorlevel 1 goto fail
echo [SEDA] Magalu ZenRows GraphQL listing probe finished
exit /b 0

:fail
echo [SEDA] Magalu ZenRows GraphQL listing probe failed
exit /b 1

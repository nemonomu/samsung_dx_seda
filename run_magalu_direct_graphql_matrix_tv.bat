@echo off
setlocal
cd /d C:\samsung_dx_seda
set SEDA_RETAILERS=magalu
set SEDA_PRODUCT_LINE=TV
set SEDA_MAGALU_DIRECT_MATRIX_PAGES=1,2,3
echo [SEDA] Magalu TV direct GraphQL matrix started
python -m seda.magalu.probe_direct_graphql_matrix TV
if errorlevel 1 goto fail
echo [SEDA] Magalu TV direct GraphQL matrix finished
exit /b 0

:fail
echo [SEDA] Magalu TV direct GraphQL matrix failed
exit /b 1

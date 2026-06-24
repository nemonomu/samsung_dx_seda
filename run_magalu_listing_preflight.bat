@echo off
setlocal
cd /d C:\samsung_dx_seda
set SEDA_RETAILERS=magalu
set SEDA_FETCH_MODE=magalu_graphql_first
set SEDA_MAGALU_PREFLIGHT_PAGES=1,2,3
python -m seda.magalu.preflight_listing_transport %*
endlocal

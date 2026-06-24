@echo off
setlocal
cd /d C:\samsung_dx_seda
set SEDA_RETAILERS=magalu
set SEDA_MAGALU_DIRECT_MATRIX_PAGES=1,2,3
python -m seda.magalu.probe_direct_graphql_matrix %*
endlocal

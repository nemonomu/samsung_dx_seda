@echo off
setlocal
cd /d C:\samsung_dx_seda

set SEDA_PRODUCT_LINE=TV
set SEDA_RETAILERS=magalu
set SEDA_FETCH_MODE=magalu_graphql_first
set SEDA_MAIN_PAGE_LIST=1,2,3
set SEDA_RUN_ROOT=C:\samsung_dx_seda\seda\data\magalu\listing_test\tv_3page

python -m seda.magalu.step01_main_list
endlocal

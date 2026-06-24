@echo off
setlocal
cd /d "%~dp0"

if not defined SEDA_PRODUCT_LINE set SEDA_PRODUCT_LINE=TV
if not defined SEDA_MAGALU_UC_NEXTDATA_PAGES set SEDA_MAGALU_UC_NEXTDATA_PAGES=1,2,3

python -m seda.magalu.probe_uc_nextdata_listing --product-line %SEDA_PRODUCT_LINE% --pages %SEDA_MAGALU_UC_NEXTDATA_PAGES%
endlocal

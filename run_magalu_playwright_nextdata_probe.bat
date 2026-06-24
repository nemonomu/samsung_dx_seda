@echo off
setlocal
cd /d "%~dp0"

if not defined SEDA_PRODUCT_LINE set SEDA_PRODUCT_LINE=TV
if not defined SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_PAGES set SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_PAGES=1,2,3
if not defined SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_CHANNEL set SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_CHANNEL=chrome

python -m seda.magalu.probe_playwright_nextdata_listing --product-line %SEDA_PRODUCT_LINE% --pages %SEDA_MAGALU_PLAYWRIGHT_NEXTDATA_PAGES%
endlocal

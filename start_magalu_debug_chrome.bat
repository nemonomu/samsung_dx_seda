@echo off
setlocal

set MAGALU_DEBUG_PORT=9222
set MAGALU_DEBUG_PROFILE=C:\tmp\seda_magalu_real_profile
set MAGALU_CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe

if not exist "%MAGALU_CHROME%" set MAGALU_CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
if not exist "%MAGALU_CHROME%" (
  echo [SEDA] Chrome executable not found.
  exit /b 1
)

if not exist "%MAGALU_DEBUG_PROFILE%" mkdir "%MAGALU_DEBUG_PROFILE%"

echo [SEDA] Starting Chrome debug session on port %MAGALU_DEBUG_PORT%
echo [SEDA] Profile: %MAGALU_DEBUG_PROFILE%
start "" "%MAGALU_CHROME%" --remote-debugging-port=%MAGALU_DEBUG_PORT% --user-data-dir="%MAGALU_DEBUG_PROFILE%" "https://www.magazineluiza.com.br/busca/tv/"

echo.
echo [SEDA] After Magalu opens normally in this Chrome window, run:
echo set SEDA_MAGALU_BROWSER_ADDRESS=127.0.0.1:%MAGALU_DEBUG_PORT%
echo python -m seda.magalu.probe_collection_flow --product-lines TV REF LDY --listing-pages 1 --detail-limit 5 --review-limit 3 --pdp-html-limit 1 --timeout 25 --html-timeout 20

exit /b 0

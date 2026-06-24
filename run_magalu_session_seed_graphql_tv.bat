@echo off
setlocal
cd /d C:\samsung_dx_seda
set SEDA_RETAILERS=magalu
set SEDA_PRODUCT_LINE=TV
set SEDA_MAGALU_SESSION_SEED_PAGES=1,2,3
echo [SEDA] Magalu TV session-seeded direct GraphQL probe started
python -m seda.magalu.probe_session_seed_graphql TV
if errorlevel 1 goto fail
echo [SEDA] Magalu TV session-seeded direct GraphQL probe finished
exit /b 0

:fail
echo [SEDA] Magalu TV session-seeded direct GraphQL probe failed
exit /b 1

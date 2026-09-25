# Repository Instructions

- Before answering any question or starting any work in this repository, read `docs/zenrows_api_key_policy.md` and follow it.
- Never directly read, write, print, grep, copy, hash, log, or otherwise inspect a ZenRows API key value.
- Keep `.env`, `.env.*`, `seda/.env`, and `seda/.env.*` excluded from Git.
- When changing code, verify syntax, function calls, control flow, DB load/save behavior, output contracts, and regressions in existing working logic.
- Mode 1 must preserve the pre-20260922 REST collection behavior from commit 1290145: use rest_legacy, without newer price-identity validation, retry ceilings, raw provenance admission or partial-page minimums. Preserve modes 1-1/2/3/4 separately; do not apply their guards or Detail recovery to Mode 1. Diagnostic redaction/logs must not alter collection decisions. After each Casas listing change, explicitly verify and report the selected mode. The current default is 1; change it only when requested. See `docs/casas_listing_modes.md`, `docs/casas_listing_mode1_legacy.md` and `docs/casas_listing_rest_url_first.md`.

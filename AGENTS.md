# Repository Instructions

- Before answering any question or starting any work in this repository, read `docs/zenrows_api_key_policy.md` and follow it.
- Never directly read, write, print, grep, copy, hash, log, or otherwise inspect a ZenRows API key value.
- Keep `.env`, `.env.*`, `seda/.env`, and `seda/.env.*` excluded from Git.
- When changing code, verify syntax, function calls, control flow, DB load/save behavior, output contracts, and regressions in existing working logic.
- Preserve all three Casas listing modes: 1=REST API, 2=hybrid, 3=UC + browser API. After each Casas listing code change, explicitly verify the final selected mode in the batch and report it. The current default is 3; change it only when requested. See `docs/casas_listing_modes.md`.

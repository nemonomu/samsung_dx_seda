# ZenRows API Key Handling Policy

## Mandatory pre-work check

Before answering any question or starting any task in this repository, read this file first and follow it.

## Non-negotiable rules

- Never open, print, echo, grep, copy, hash, log, serialize, or otherwise inspect the actual ZenRows API key value.
- Never create, edit, replace, rotate, or write `ZENROWS_API_KEY` on the user's behalf.
- Never hard-code the key in Python, batch files, PowerShell, tests, fixtures, documentation, command-line arguments, URLs, traces, or artifacts.
- Application code must obtain the key only through the existing environment-loading contract and use it without exposing the value.
- Business modules must use the centralized ZenRows client. They must not read an env file or handle the raw key directly.
- A missing or invalid key may be reported only as a boolean/status error such as `key_missing`; the value or a derivative of it must never be shown.
- Every supported ZenRows proxy request must use `proxy_country=br`. The centralized client enforces this value after all profile, environment, and caller options, so callers must not override it.

## Approved loading contract

1. `seda.step00_config.load_env()` loads configuration into the process environment.
2. An explicitly configured `SEDA_ENV_PATH` has priority. Otherwise the candidates are `seda/.env`, then the repository-root `.env`.
3. `seda.magalu.zenrows_client` obtains the key through the environment-backed centralized client.
4. Callers use the client result only; they do not receive, print, persist, or transform the raw key.

## Git exclusion contract

API-key-bearing env files are local secrets and must always remain excluded from Git. The repository `.gitignore` must continue to include all of the following:

```text
.env
.env.*
seda/.env
seda/.env.*
```

The policy document itself is tracked so future workers can read it; secret env files are not tracked.

## Safe grep audit

Only inspect filenames or source-code references outside secret files. Never grep the contents of `.env` files.

```powershell
rg -l "ZENROWS_API_KEY|ZENROWS_APIKEY" . --glob "!*.env" --glob "!*.env.*" --glob "!*.log"
rg -n "^\.env$|^\.env\.\*$|^seda/\.env$|^seda/\.env\.\*$" .gitignore
```

If a command could display an API-key value, do not run it.

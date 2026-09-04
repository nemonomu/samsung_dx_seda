# SEDA retail crawler

Crawler scaffold for SEDA Brazil TV retail.com collection.

Targets from `erd.xlsx`:

- Magalu: `https://www.magazineluiza.com.br/busca/tv/`
- Casas Bahia: `https://www.casasbahia.com.br/tv/b`
- ZIP/postal context: `01010-010`
- Search sort: relevance, 300 visible SKUs
- Reviews: top 20 visible reviews per product

The pipeline prefers structured page data when available, then rendered HTML through
ZenRows or UC when the site blocks plain requests.

## Full Run

```powershell
python -m seda.magalu.magalu_orchestrator --all --product-line TV
python -m seda.casas_bahia.casas_bahia_orchestrator --all --product-line TV
```

`--all` is the operational full run. It includes collection, final CSV creation,
field audit, optional S3 sync, DB table preparation/load, status check, email
notification, and optional local cleanup.

Operational `--all` starts from step 01. Run step 00 only when regenerating ERD
config:

```powershell
python -m seda.magalu.magalu_orchestrator --all --include-setup --product-line TV
python -m seda.magalu.magalu_orchestrator 00 --product-line TV
```

For Magalu TV production runs, set the runtime context in `.env`:

```text
SEDA_RETAILERS=magalu
SEDA_ACTIVE_RETAILER=magalu
SEDA_PRODUCT_LINE=TV
SEDA_EMAIL_NOTIFY=1
SEDA_EMAIL_DRY_RUN=0
```

You can also pass the product line explicitly:

```powershell
python -m seda.magalu.magalu_orchestrator --product-line TV --all
```

DB insert is controlled by `DB_CONFIG` and `SEDA_DB_FINAL_TABLE` or
`SEDA_OUTPUT_TABLE`. Product-line-specific table overrides are also supported:

```text
SEDA_DB_FINAL_TABLE_TV=tv_retail_com_seda
SEDA_DB_FINAL_TABLE_REF=ref_retail_com_seda
SEDA_DB_FINAL_TABLE_LDY=ldy_retail_com_seda
```

Email notification is sent by `step10_status_check` after DB load. A full run is
successful only when the final CSV has rows, DB inserted row count equals final
CSV row count, and email status is `sent`.

Useful test run:

```powershell
$env:SEDA_PAGES='1'
$env:SEDA_TARGET_SIZE='10'
$env:SEDA_DETAIL_LIMIT='3'
python -m seda.seda_orchestrator --retailer magalu --all
python -m seda.seda_orchestrator --retailer casas_bahia --all
```

The shared launcher is only a dispatcher. `--retailer all` runs the Magalu and
Casas Bahia orchestrators as separate child processes with separate
`SEDA_RETAILERS`, `SEDA_ACTIVE_RETAILER`, and dated `SEDA_RUN_ROOT` values:

```powershell
python -m seda.seda_orchestrator --retailer all --product-line TV --all
```

When an explicit `SEDA_RUN_ROOT` base is provided to `--retailer all`, child
results are written below
`<SEDA_RUN_ROOT>/<retailer>/<product-line>` instead of sharing one directory.

No mixed-retailer CSV is passed to final output or DB load. The interleaved
batch files use the same isolation at every retailer/product-line stage:

```text
run_magalu_casas_interleaved_tv_ref_ldy_full.bat
run_magalu_casas_interleaved_ref_ldy_full.bat
run_magalu_casas_ref_ldy_seq.bat
```

These combined batch files force the canonical dated run root for every stage,
so a stale `SEDA_RUN_ROOT` from the shell or `.env` cannot merge their outputs.

If `SEDA_DB_TRUNCATE_BEFORE_LOAD=1`, a combined run converts that destructive
table-wide operation into a retailer-and-product-line-scoped replacement. Each
retailer deletes its own canonical and legacy `account_name` rows for the
active TV/REF/LDY line and inserts the new rows in one transaction. A failed
insert rolls back that retailer's delete, and the other retailer or product
line is never removed. With the default truncate setting (`0`), the existing
append/history behavior is unchanged.

Local cleanup remains opt-in. When enabled, it only considers dated run
directories in the supported legacy and split layouts (`data/<date>`,
`data/<product-line>/<date>`, and
`data/<retailer>/<product-line>/<date>`). It skips cleanup for an external
`SEDA_RUN_ROOT`, rejects negative retention, and refuses any run tree that
contains a symbolic link, junction, or other reparse point. Its manifest
records validation failures and any reparse paths skipped during discovery.

Run one step:

```powershell
python -m seda.seda_orchestrator --retailer magalu 01
python -m seda.seda_orchestrator --retailer casas_bahia detail_enrichment
```

Step numbers are retailer-specific because Casas Bahia has additional
backfill steps, and the historical shared-orchestrator numbering no longer
applies. Select one retailer when using numeric identifiers; prefer step names.
The dispatcher rejects numeric identifiers with `--retailer all` so different
retailer steps cannot be run accidentally. Named steps used with
`--retailer all` must exist in both pipelines; select `casas_bahia` explicitly
for `freight_cdp_backfill` or `listing_discount_backfill`.

Resume incomplete work:

```powershell
python -m seda.seda_orchestrator --retailer magalu --resume
python -m seda.seda_orchestrator --retailer casas_bahia --resume
```

## Steps

Magalu operational steps:

```text
00 erd_schema
01 main_list
02 main_targets
03 bsr_list
04 bsr_rank
05 final_targets
06 detail_enrichment
07 review20
08 final_output
09 field_audit
10 s3_sync
11 db_prepare
12 db_load
13 status_check
14 local_cleanup
```

Casas Bahia operational steps:

```text
00 erd_schema
01 main_list
02 main_targets
03 bsr_list
04 bsr_rank
05 final_targets
06 detail_enrichment
07 freight_cdp_backfill
08 review20
09 listing_discount_backfill
10 final_output
11 field_audit
12 s3_sync
13 db_prepare
14 db_load
15 status_check
16 local_cleanup
```

## Environment

Place `.env` in the project root or the `seda` folder:

```text
C:\samsung_dx_seda\.env
seda/.env
```

The loader reads `seda/.env` first and then project-root `.env`. The `.env` file
is ignored by `.gitignore`; do not commit it.

Detail GraphQL trace cleanup is enabled by default and is independent from
whole-run cleanup:

```text
SEDA_DETAIL_TRACE_CLEANUP=1
SEDA_DETAIL_TRACE_RETENTION_DAYS=3
```

The `local_cleanup` step removes only recognized files under `detail/trace`
after the whole trace bundle has been unchanged for at least 72 hours. The
current run, raw payloads, output CSVs, and unrecognized files are preserved.
Per-file cleanup failures are recorded in the cleanup manifest and do not fail
the crawler batch.
`SEDA_LOCAL_CLEANUP` continues to control whole dated-run deletion separately
and remains disabled by default.

The integrated TV/REF/LDY runner enables a stricter local storage policy:

```text
SEDA_LOCAL_CLEANUP=1
SEDA_LOCAL_RETENTION_DAYS=3
SEDA_MAGALU_PROFILE_CLEANUP=1
SEDA_MAGALU_PROFILE_RETENTION_HOURS=48
SEDA_STORAGE_MIN_FREE_GB=2
```

Before collection, expired dated runs and stale Magalu browser profiles are
removed and the profile drive must have at least 2 GiB free. The current
timestamped base/worker profiles are removed after the integrated runner ends.
Hard-killed runs are recovered by the 48-hour stale-profile cleanup on the next
start. Only the same integrated-runner profile series, with managed timestamped
names directly below `C:\tmp\seda_magalu_profiles`, is eligible. Other profile
series, custom paths, and reparse points are rejected, and a locked active
profile cannot pass the atomic rename step.

`erd.xlsx` is also ignored by git. For a clean RDP setup, place it at:

```text
C:\samsung_dx_seda\erd.xlsx
```

Set `SEDA_ERD_PATH` only if the ERD is stored elsewhere.

## Product Scope

Runtime folders are separated by retailer, product line, and run date:

```text
seda/data/magalu/tv/YYYYMMDD
seda/data/magalu/ref/YYYYMMDD
seda/data/magalu/ldy/YYYYMMDD
```

Magalu supports product-line-specific listing URLs. Defaults are:

```text
TV  main https://www.magazineluiza.com.br/busca/tv/
TV  bsr  https://www.magazineluiza.com.br/busca/tv/?sortType=soldQuantity&sortOrientation=desc
REF main https://www.magazineluiza.com.br/busca/geladeira/
REF bsr  https://www.magazineluiza.com.br/busca/geladeira/?page=1&sortOrientation=desc&sortType=soldQuantity
LDY main https://www.magazineluiza.com.br/busca/maquina+de+lavar/
LDY bsr  https://www.magazineluiza.com.br/busca/maquina+de+lavar/?page=1&sortOrientation=desc&sortType=soldQuantity
```

Override them in `.env` only when needed:

```text
SEDA_MAGALU_MAIN_URL_REF=
SEDA_MAGALU_BSR_URL_REF=
SEDA_MAGALU_MAIN_URL_LDY=
SEDA_MAGALU_BSR_URL_LDY=
```

Run by product line:

```powershell
python -m seda.magalu.magalu_orchestrator --product-line TV --all
python -m seda.magalu.magalu_orchestrator --product-line REF --all
python -m seda.magalu.magalu_orchestrator --product-line LDY --all
```

## HAR Capture

For Magalu/Casas Bahia API discovery, open DevTools before loading the page:

```text
Network tab -> Preserve log on -> Disable cache on -> All requests selected
Load/reload the page -> wait until products are visible -> Save all as HAR
```

Needed pages:

```text
https://www.magazineluiza.com.br/busca/tv/
https://www.casasbahia.com.br/tv/b
```

The current crawler includes `python -m seda.har_probe <har files...>` to summarize
candidate API/product arrays and GraphQL operation names without printing cookies or
authorization headers.

Automatic browser capture is also available when a manual HAR is inconvenient:

```powershell
python -m seda.network_capture `
  "https://www.casasbahia.com.br/tv/b" `
  "https://www.casasbahia.com.br/tv/b?ordenacao=maisvendidos" `
  --output-dir seda\data\network_capture\casas_tv
```

It writes:

```text
capture.har.json
api_summary.json
graphql_summary.json
```

Use `graphql_summary.json` to check endpoint, `operationName`, variable keys, and
whether an array/batch GraphQL payload was observed.

For detail and review discovery, capture one product with many reviews per retailer:

```text
1. Open DevTools before opening the product detail page.
2. Network -> Preserve log ON -> Disable cache ON -> All selected.
3. Load the product detail page.
4. Wait until rating/review summary and similar products render.
5. Click the review entry point such as "Ver todas as avaliações",
   "Ver mais avaliações", "Avaliações", or "See all reviews".
6. If pagination or "load more" exists, move to the next review page or click it
   until at least 20 reviews have appeared.
7. Save all as HAR with content.
```

Suggested Magalu detail seed:

```text
https://www.magazineluiza.com.br/smart-tv-40-aoc-full-hd-dled-serie-5045-40s5045-78g-roku-tv-2024/p/240949800/et/elit/?seller_id=magazineluiza
```

## Translation

ERD-marked structured Portuguese fields are translated after collection when CSV
outputs are written. Current dictionary-backed fields:

```text
sku_status
discount_type
delivery_availability
pick_up_availability
recommendation_intent
```

Set `SEDA_TRANSLATE_OUTPUT=0` only when raw Portuguese values are needed for parser
debugging.

## Transport

Default mode is UC first, then ZenRows, then plain requests:

```powershell
$env:SEDA_FETCH_MODE='uc_first'
```

Or force ZenRows first:

```powershell
$env:ZENROWS_API_KEY='...'
$env:SEDA_FETCH_MODE='zenrows_first'
```

The shared dispatcher preserves these generic modes for both retailers. Use
`SEDA_MAGALU_FETCH_MODE` or `SEDA_CASAS_BAHIA_FETCH_MODE` when each retailer
needs a different transport mode.

Or force UC only:

```powershell
$env:SEDA_FETCH_MODE='uc'
```

If neither is set, the crawler tries `requests`, which is useful for parser tests
but may be blocked by the retailer.

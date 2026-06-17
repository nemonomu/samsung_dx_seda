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
python -m seda.seda_orchestrator --all
```

Run one step:

```powershell
python -m seda.seda_orchestrator 01
python -m seda.seda_orchestrator detail_enrichment
```

Resume incomplete work:

```powershell
python -m seda.seda_orchestrator --resume
```

## Steps

```text
00 erd_schema
01 main_list
02 main_targets
03 bsr_list
04 bsr_rank
05 promotion_deals
06 trending_deals
07 final_targets
08 detail_enrichment
09 review20
10 final_output
11 field_audit
12 s3_sync
13 db_prepare
14 db_load
15 status_check
16 local_cleanup
```

Casas Bahia adds two retailer-specific steps between `review20` and
`final_output`:

```text
10 freight_cdp_backfill
11 listing_badge_backfill
12 final_output
13 field_audit
14 s3_sync
15 db_prepare
16 db_load
17 status_check
18 local_cleanup
```

## Environment

Place `.env` in the project root or the `seda` folder:

```text
C:\samsung_dx_seda\.env
seda/.env
```

The loader reads `seda/.env` first and then project-root `.env`. The `.env` file
is ignored by `.gitignore`; do not commit it.

`erd.xlsx` is also ignored by git. For a clean RDP setup, place it at:

```text
C:\samsung_dx_seda\erd.xlsx
```

Set `SEDA_ERD_PATH` only if the ERD is stored elsewhere.

## Product Scope

The current Magalu implementation targets TV. Runtime folders are separated by
retailer, product line, and run date:

```text
seda/data/magalu/tv/YYYYMMDD
seda/data/magalu/ref/YYYYMMDD
seda/data/magalu/ldy/YYYYMMDD
```

Future SEDA product modules such as REF and LDY should add their own URL,
parser-filter, and field rules while reusing the same orchestrator and DB/mail
steps.

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

Or force UC only:

```powershell
$env:SEDA_FETCH_MODE='uc'
```

If neither is set, the crawler tries `requests`, which is useful for parser tests
but may be blocked by the retailer.

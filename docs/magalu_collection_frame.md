# Magalu Collection Frame

## Goal

Collect listing, detail, availability, similar products, rating summary, and review content with the fastest stable path and minimum paid fallback usage.

## Primary Strategy

Use a real browser session once, then replay Magalu GraphQL requests inside the page context.

Reason:
- Direct HTTP requests to Magalu listing HTML, static JS, and GraphQL can return Akamai 403.
- Browser navigation to the listing page can establish a usable session.
- GraphQL requests made from that browser page context return stable JSON for listing, detail, and reviews.

## Listing Frame

Primary:
- Warm up browser on the listing URL.
- Call `searchQuery` from page context through `federation.magazineluiza.com.br/graphql`.
- Convert the GraphQL `search.products` result into the existing `__NEXT_DATA__` parser shape.

Fields expected from listing:
- product URL
- item
- retailer SKU name
- original and final price
- sku status, including sponsored/Patrocinado
- discount type
- listing delivery and pickup tag when present
- initial screen size/model year from title
- main_rank or bsr_rank

Rank rule:
- Keep first-seen rank.
- Later duplicated products must not overwrite the earlier main_rank or bsr_rank.

## Detail Frame

Primary:
- Use browser-context GraphQL `itemQuery`.

Fields expected from item detail:
- SKU/model from factsheet Modelo when available
- title
- original and final price
- screen size
- annual electricity or energy-related factsheet value
- model year
- rating count and score when exposed on item

Availability:
- Use browser-context GraphQL `shippingQuery`.
- Store delivery and pickup separately.
- Shipping errors are field-level failures and must not drop the SKU.

Similar products:
- Use browser-context GraphQL `showcaseQuery`.
- Store names with the same JSON delimiter style used elsewhere.

## Review Frame

Primary:
- Use browser-context GraphQL `ProductRating`.
- Collect up to 20 review descriptions.
- Store rating summary fields from `general`.

Rules:
- If a product has no rating/reviews, record field-level miss and continue.
- Review failure must not fail the whole SKU.

## PDP HTML Frame

Use PDP HTML only for fields not reliably available through GraphQL:
- AI review summary
- rendered similar product section if GraphQL is incomplete
- any field proven to exist only in rendered `__NEXT_DATA__`

Primary:
- Browser-context HTML fetch or browser navigation.

Fallback:
- ZenRows only after explicit approval.

## ZenRows Policy

Use ZenRows only when local browser/session paths fail due to bot detection or missing rendered-only data.

Before every paid call, report:
- target URL type
- profile and enabled features
- planned call count
- expected cost multiplier
- estimated cost
- reason the call is needed

Do not run paid ZenRows calls before approval.

## Current Default Fallback Order

1. Browser session warmup
2. Browser-context GraphQL or browser-context HTML fetch
3. Local direct fallback only when explicitly useful
4. ZenRows after approval

## Validation Minimum

Before a run is treated as stable:
- listing page smoke test returns rows with prices and listing tags
- one item detail smoke test returns item/factsheet fields
- one product with reviews returns rating summary and review descriptions
- output QA checks missing columns and constant suspicious values

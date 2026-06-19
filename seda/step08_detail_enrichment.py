import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .parsers import compact_json, parse_detail, sku_from_url
from .step00_config import RETAILERS, OUTPUT_COLUMNS, read_csv, run_root, write_csv
from .transport import fetch_url, is_blocked_html


def _base_url(retailer):
    for config in RETAILERS.values():
        if config.name == retailer:
            return config.base_url
    return ""


def _review_count(value):
    if not value:
        return 0
    try:
        parsed = json.loads(value)
    except ValueError:
        return 1
    return len(parsed) if isinstance(parsed, list) else 0


def _merge_magalu_reviews(row, product_url):
    if row.get("retailer") != "Magalu":
        return None
    if os.getenv("SEDA_MAGALU_REVIEW_GRAPHQL", "1").lower() in {"0", "false", "no", "n"}:
        return None
    if _review_count(row.get("detailed_review_content")) >= int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20")):
        return None
    if os.getenv("SEDA_MAGALU_SKIP_REVIEW_WITHOUT_RATING", "1").lower() not in {"0", "false", "no", "n"}:
        if not row.get("star_rating") and not row.get("count_of_star_ratings"):
            row["parse_status"] = _append_token(row.get("parse_status", ""), "reviews_skipped_no_rating")
            return None

    from .magalu.review_api import fetch_product_rating

    limit = int(os.getenv("SEDA_MAGALU_REVIEW_LIMIT", "20"))
    try:
        review_count = int(float(str(row.get("count_of_reviews") or "").replace(".", "").replace(",", ".")))
    except ValueError:
        review_count = -1
    if review_count >= 0:
        limit = min(limit, review_count)
    if limit <= 0:
        return None
    result = fetch_product_rating(sku_from_url(product_url) or row.get("sku"), limit=limit)
    reviews = result.get("reviews") or []
    if reviews:
        row["detailed_review_content"] = compact_json(reviews)
        row["fetch_method"] = _append_token(row.get("fetch_method", ""), result.get("method", "graphql_product_rating"))
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_{len(reviews)}")

    general = result.get("general") or {}
    if general:
        row["star_rating"] = general.get("rating", "") or row.get("star_rating", "")
        row["count_of_star_ratings"] = general.get("reviewCount", "") or row.get("count_of_star_ratings", "")
        if general.get("commentCount") is not None:
            row["count_of_reviews"] = general.get("commentCount")
        else:
            row["count_of_reviews"] = general.get("reviewCount", "") or row.get("count_of_reviews", "")
    return result


def _merge_casas_bahia_apis(row):
    if row.get("retailer") != "Casas Bahia":
        return
    if os.getenv("SEDA_CASAS_BAHIA_API_ENRICH", "1").lower() in {"0", "false", "no", "n"}:
        return

    product_id = row.get("retailer_product_id", "")
    sku_id = sku_from_url(row.get("product_url", "")) or row.get("sku", "")
    seller_id = row.get("seller_id", "") or os.getenv("SEDA_CASAS_BAHIA_DEFAULT_SELLER_ID", "10037")

    try:
        from .casas_bahia.detail_api import fetch_freight, fetch_pickup, fetch_product_source, fetch_similar_names

        if os.getenv("SEDA_CASAS_BAHIA_PRODUCT_SOURCE_API", "1").lower() not in {"0", "false", "no", "n"}:
            product_source = fetch_product_source(sku_id)
            if product_source.get("success"):
                _merge_non_empty(row, product_source.get("detail") or {})
                row["fetch_method"] = _append_token(
                    row.get("fetch_method", ""), product_source.get("method", "casas_bahia_product_source_api")
                )
                cost = (product_source.get("headers") or {}).get("X-Request-Cost", "")
                if cost:
                    row["parse_status"] = _append_token(row.get("parse_status", ""), f"product_source_cost:{cost}")
            else:
                row["parse_status"] = _append_token(
                    row.get("parse_status", ""), f"product_source_failed:{product_source.get('error','unknown')}"
                )

        product_id = row.get("retailer_product_id", "") or product_id

        if os.getenv("SEDA_CASAS_BAHIA_FREIGHT_API", "1").lower() not in {"0", "false", "no", "n"}:
            freight = fetch_freight(sku_id, seller_id)
            if freight.get("success"):
                _merge_non_empty(row, freight.get("detail") or {})
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_freight_api")
            else:
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"freight_api_failed:{freight.get('error','unknown')}")

        if os.getenv("SEDA_CASAS_BAHIA_PICKUP_API", "1").lower() not in {"0", "false", "no", "n"}:
            pickup = fetch_pickup(sku_id, seller_id)
            if pickup.get("success"):
                _merge_non_empty(row, pickup.get("detail") or {})
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_pickup_api")
            else:
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"pickup_api_failed:{pickup.get('error','unknown')}")

        if os.getenv("SEDA_CASAS_BAHIA_RECS_API", "1").lower() not in {"0", "false", "no", "n"}:
            similar = fetch_similar_names(product_id, sku_id=sku_id, current_product=row)
            if similar.get("success") and similar.get("names"):
                row["retailer_sku_name_similar"] = compact_json(similar.get("names"))
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_recs_api")
            elif similar.get("success"):
                row["parse_status"] = _append_token(
                    row.get("parse_status", ""),
                    f"recs_empty:{similar.get('source_count', 0)}:{similar.get('filtered_count', 0)}",
                )
            elif not similar.get("success"):
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"recs_api_failed:{similar.get('error','unknown')}")
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"casas_bahia_detail_api_error:{type(exc).__name__}")

    if os.getenv("SEDA_CASAS_BAHIA_REVIEW_API", "1").lower() in {"0", "false", "no", "n"}:
        return
    if _review_count(row.get("detailed_review_content")) >= int(os.getenv("SEDA_CASAS_BAHIA_REVIEW_LIMIT", "20")):
        return
    try:
        from .casas_bahia.review_api import fetch_reviews

        result = fetch_reviews(product_id)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_api_error:{type(exc).__name__}")
        return
    if result.get("success"):
        general = result.get("general") or {}
        reviews = result.get("reviews") or []
        summary = result.get("summary") or general.get("summary", "")
        if summary and not row.get("summarized_review_content"):
            row["summarized_review_content"] = summary
        if reviews:
            row["detailed_review_content"] = compact_json(reviews)
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_reviews_api")
            seen = int(result.get("review_items_seen") or len(reviews))
            status = f"reviews_{len(reviews)}/{seen}" if seen != len(reviews) else f"reviews_{len(reviews)}"
            row["parse_status"] = _append_token(row.get("parse_status", ""), status)
        _merge_zero_preserving(row, "star_rating", general.get("rating", ""))
        _merge_zero_preserving(row, "count_of_star_ratings", general.get("ratingQty", ""))
        _merge_zero_preserving(row, "count_of_reviews", general.get("ratingQty", ""))
        if general.get("recommendationPercentage") not in ("", None):
            row["recommendation_intent"] = f"{general.get('recommendationPercentage')}% dos clientes recomendam esse produto"
    else:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"reviews_api_failed:{result.get('error','unknown')}")
    if not row.get("recommendation_intent"):
        row["recommendation_intent"] = "0% dos clientes recomendam esse produto"
        row["parse_status"] = _append_token(row.get("parse_status", ""), "recommendation_default_0")


def _clear_magalu_listing_metrics(row):
    if row.get("retailer") != "Magalu":
        return
    for key in ("star_rating", "count_of_star_ratings", "count_of_reviews"):
        row[key] = ""


def _merge_non_empty(row, detail):
    row.update({key: value for key, value in detail.items() if value not in ("", None, [], {})})


def _merge_zero_preserving(row, key, value):
    if value in ("", None, [], {}):
        return
    row[key] = _metric_text(value)


def _metric_text(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    if number == 0:
        return "0"
    return f"{number:g}"


def _magalu_graphql_detail(row, product_url):
    if row.get("retailer") != "Magalu":
        return None
    if os.getenv("SEDA_MAGALU_DETAIL_GRAPHQL", "1").lower() in {"0", "false", "no", "n"}:
        return None

    from .magalu.detail_api import fetch_detail

    item_id = sku_from_url(product_url) or row.get("item") or row.get("sku")
    result = fetch_detail(item_id)
    if not result.get("success"):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"detail_graphql_failed:{result.get('error','unknown')}")
        return result
    _merge_non_empty(row, result.get("detail") or {})
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "graphql_item")
    row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_item_graphql")
    return result


def _merge_magalu_zenrows_detail(row, product_url):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_ZENROWS_DETAIL_FALLBACK", "1").lower() in {"0", "false", "no", "n"}:
        return False
    try:
        from .magalu.zenrows_client import fetch_pdp_rendered_html

        result = fetch_pdp_rendered_html(product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"zenrows_detail_error:{type(exc).__name__}")
        return False
    token = f"zenrows_detail:{result.profile}:{result.estimated_multiplier}"
    if result.error:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:{result.error}")
        return False
    detail = parse_detail(result.text or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    meaningful_keys = (
        "retailer_sku_name",
        "final_sku_price",
        "screen_size",
        "model_year",
        "ref_refrigerator_type",
        "ref_capacity",
        "ldy_loading_type",
        "ldy_capacity",
        "delivery_availability",
        "pick_up_availability",
        "summarized_review_content",
        "count_of_star_ratings",
        "count_of_reviews",
    )
    if not any(detail.get(key) for key in meaningful_keys):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:empty_detail")
        return False
    _merge_non_empty(row, detail)
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), token)
    cost = (result.headers or {}).get("X-Request-Cost", "")
    status = "zenrows_detail_html" if not cost else f"zenrows_detail_html_cost:{cost}"
    row["parse_status"] = _append_token(row.get("parse_status", ""), status)
    return True

def _retry_magalu_shipping_blanks(row, product_url):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY", "1").lower() in {"0", "false", "no", "n"}:
        return False
    if not _needs_magalu_shipping_retry(row):
        return False

    from .magalu.detail_api import fetch_shipping_for_item_id

    item_id = sku_from_url(product_url) or row.get("item") or row.get("sku")
    attempts = int(os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY_ATTEMPTS", "1"))
    for attempt in range(1, attempts + 1):
        result = fetch_shipping_for_item_id(item_id)
        if result.get("delivery") and not row.get("delivery_availability"):
            row["delivery_availability"] = result["delivery"]
        if result.get("pickup") and not row.get("pick_up_availability"):
            row["pick_up_availability"] = result["pickup"]
        if result.get("delivery") or result.get("pickup"):
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), "shipping_blank_retry")
            row["parse_status"] = _append_token(row.get("parse_status", ""), "shipping_blank_retry")
            return True
        if attempt == attempts:
            row["parse_status"] = _append_token(
                row.get("parse_status", ""),
                f"shipping_blank_retry_failed:{result.get('error', 'empty_shipping')}",
            )
    return False


def _needs_magalu_shipping_retry(row):
    return row.get("retailer") == "Magalu" and (not row.get("delivery_availability") or not row.get("pick_up_availability"))


def _backfill_magalu_shipping_blanks(rows, output, checkpoint_every=25):
    if os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY", "1").lower() in {"0", "false", "no", "n"}:
        return rows
    candidates = [row for row in rows if _needs_magalu_shipping_retry(row)]
    limit = int(os.getenv("SEDA_MAGALU_SHIPPING_BLANK_RETRY_LIMIT", "0"))
    if limit:
        candidates = candidates[:limit]
    total = len(candidates)
    if not total:
        return rows
    print(f"[seda] shipping blank retry candidates={total}", flush=True)
    updated = 0
    for index, row in enumerate(candidates, start=1):
        before = (row.get("delivery_availability", ""), row.get("pick_up_availability", ""))
        changed = _retry_magalu_shipping_blanks(row, row.get("product_url", ""))
        after = (row.get("delivery_availability", ""), row.get("pick_up_availability", ""))
        if changed and after != before:
            updated += 1
        print(
            f"[seda] shipping retry {index}/{total} item={_safe_log_value(row.get('item'))} "
            f"updated={int(changed and after != before)}",
            flush=True,
        )
        if checkpoint_every and index % checkpoint_every == 0:
            write_csv(output, rows, columns=OUTPUT_COLUMNS)
            print(f"[seda] shipping retry checkpoint {output} updated={updated}", flush=True)
    print(f"[seda] shipping blank retry updated={updated}/{total}", flush=True)
    return rows


def _merge_magalu_pdp_html(row, product_url):
    if row.get("retailer") != "Magalu":
        return
    needs_summary = not row.get("summarized_review_content") and any(
        row.get(key) for key in ("star_rating", "count_of_star_ratings", "count_of_reviews", "detailed_review_content")
    )
    needs_similar = not row.get("retailer_sku_name_similar")
    if not needs_summary and not needs_similar:
        return
    if os.getenv("SEDA_MAGALU_PDP_HTML_FETCH", "1").lower() in {"0", "false", "no", "n"}:
        return
    try:
        from .magalu.browser_session import fetch_html

        result = fetch_html(product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"pdp_html_error:{type(exc).__name__}")
        return
    if result.get("status_code") != 200 or "__NEXT_DATA__" not in (result.get("text") or ""):
        text = result.get("text") or ""
        row["parse_status"] = _append_token(
            row.get("parse_status", ""),
            f"pdp_html_failed:{result.get('status_code', 0)}:len={len(text)}:next={int('__NEXT_DATA__' in text)}",
        )
        if _merge_magalu_zenrows_pdp_html(row, product_url):
            return
        return
    detail = parse_detail(result.get("text") or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    for key in ("summarized_review_content", "retailer_sku_name_similar"):
        if detail.get(key) and not row.get(key):
            row[key] = detail[key]
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "browser_pdp_html")
    row["parse_status"] = _append_token(row.get("parse_status", ""), "pdp_html")


def _merge_magalu_zenrows_pdp_html(row, product_url):
    if row.get("retailer") != "Magalu":
        return False
    if os.getenv("SEDA_MAGALU_ZENROWS_PDP_FALLBACK", "0").lower() not in {"1", "true", "yes", "y"}:
        return False
    try:
        from .magalu.zenrows_client import fetch_next_data_html

        result = fetch_next_data_html(product_url)
    except Exception as exc:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"zenrows_pdp_error:{type(exc).__name__}")
        return False
    token = f"zenrows_pdp:{result.profile}:{result.estimated_multiplier}"
    if result.error:
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:{result.error}")
        return False
    if not result.success or "__NEXT_DATA__" not in (result.text or ""):
        row["parse_status"] = _append_token(row.get("parse_status", ""), f"{token}:missing_next_data")
        return False
    detail = parse_detail(result.text or "", row.get("retailer", ""), _base_url(row.get("retailer", "")), product_url)
    merged = False
    for key in ("summarized_review_content", "retailer_sku_name_similar"):
        if detail.get(key) and not row.get(key):
            row[key] = detail[key]
            merged = True
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), token)
    row["parse_status"] = _append_token(row.get("parse_status", ""), "zenrows_pdp_html")
    return merged


def _detail_fetch_url(row, product_url):
    if row.get("retailer") != "Casas Bahia":
        return product_url
    parsed = urlsplit(product_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("frete", os.getenv("SEDA_POSTAL_CODE", "01010-010"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _fallback_fetch_enabled_for_row(row, fallback_fetch):
    if not fallback_fetch:
        return False
    if row.get("retailer") == "Magalu":
        return os.getenv("SEDA_MAGALU_DETAIL_HTML_FALLBACK", "0").lower() in {"1", "true", "yes", "y"}
    if row.get("retailer") != "Casas Bahia":
        return True
    return os.getenv("SEDA_CASAS_BAHIA_PDP_HTML_FETCH", "0").lower() in {"1", "true", "yes", "y"}


def _has_blocked_graphql_trace(result):
    if not result:
        return False
    for trace_item in result.get("trace") or []:
        try:
            status_code = int(trace_item.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        error = str(trace_item.get("error") or "").lower()
        if status_code in {401, 403, 429}:
            return True
        if "blocked" in error or "invalid_json" in error or "non_json_or_blocked" in error:
            return True
    return False


def _abort_on_magalu_blocked_streak(kind, streak, threshold, output, rows):
    if threshold <= 0 or streak < threshold:
        return
    write_csv(output, rows, columns=OUTPUT_COLUMNS)
    print(f"[seda] aborting Magalu {kind}: blocked_graphql_streak={streak} checkpoint={output}", flush=True)
    raise RuntimeError(f"magalu_{kind}_blocked_graphql_streak:{streak}")


def _append_token(value, token):
    value = str(value or "").strip()
    token = str(token or "").strip()
    if not token:
        return value
    if not value:
        return token
    parts = value.split("+")
    return value if token in parts else f"{value}+{token}"


def _safe_log_value(value):
    return str(value or "").encode("ascii", "backslashreplace").decode("ascii")


def _safe_filename(value):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return text[:120] or "sku"


def main():
    root = run_root()
    input_csv = os.getenv("SEDA_DETAIL_TARGET_CSV", str(root / "output" / "seda_final_targets.csv"))
    rows = read_csv(input_csv)
    limit = int(os.getenv("SEDA_DETAIL_LIMIT", "0"))
    if limit:
        rows = rows[:limit]
    output = os.getenv("SEDA_DETAIL_OUTPUT_CSV", str(root / "output" / "final_output_enriched.csv"))
    if os.getenv("SEDA_MAGALU_SHIPPING_BACKFILL_ONLY", "0").lower() in {"1", "true", "yes", "y"}:
        checkpoint_every = int(os.getenv("SEDA_DETAIL_CHECKPOINT_EVERY", "25"))
        rows = _backfill_magalu_shipping_blanks(rows, output, checkpoint_every=checkpoint_every)
        write_csv(output, rows, columns=OUTPUT_COLUMNS)
        print(f"[seda] wrote {output} rows={len(rows)}")
        return
    skip = int(os.getenv("SEDA_DETAIL_SKIP", "0"))
    enriched = []
    if skip:
        enriched = read_csv(output)[:skip] if os.path.exists(output) else []
        rows = rows[skip:]
    total_rows = len(enriched) + len(rows)
    checkpoint_every = int(os.getenv("SEDA_DETAIL_CHECKPOINT_EVERY", "25"))
    fallback_fetch = os.getenv("SEDA_DETAIL_FALLBACK_FETCH", "1").lower() not in {"0", "false", "no", "n"}
    magalu_detail_blocked_streak = 0
    magalu_review_blocked_streak = 0
    magalu_detail_abort_threshold = int(os.getenv("SEDA_MAGALU_DETAIL_403_ABORT_THRESHOLD", "5"))
    magalu_review_abort_threshold = int(os.getenv("SEDA_MAGALU_REVIEW_403_ABORT_THRESHOLD", "5"))
    for index, row in enumerate(rows, start=len(enriched) + 1):
        url = row.get("product_url", "")
        if not url:
            row["parse_status"] = "missing_product_url"
            enriched.append(row)
            continue
        _clear_magalu_listing_metrics(row)
        graph_result = _magalu_graphql_detail(row, url)
        detail_done = bool(graph_result and graph_result.get("success"))
        if row.get("retailer") == "Magalu" and graph_result is not None:
            if detail_done:
                magalu_detail_blocked_streak = 0
            elif _has_blocked_graphql_trace(graph_result):
                magalu_detail_blocked_streak += 1
                _abort_on_magalu_blocked_streak(
                    "detail",
                    magalu_detail_blocked_streak,
                    magalu_detail_abort_threshold,
                    output,
                    enriched + [row],
                )
            else:
                magalu_detail_blocked_streak = 0
        result = None
        if not detail_done and row.get("retailer") == "Magalu":
            detail_done = _merge_magalu_zenrows_detail(row, url)
        if not detail_done and _fallback_fetch_enabled_for_row(row, fallback_fetch):
            result = fetch_url(_detail_fetch_url(row, url))
            raw_dir = root / "detail" / "raw" / row.get("retailer", "unknown").lower().replace(" ", "_")
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / f"{index:04d}_{_safe_filename(row.get('sku') or 'sku')}.html"
            raw_path.write_text(result.text or result.error, encoding="utf-8", errors="ignore")
            blocked = is_blocked_html(result.text, result.status_code)
            if result.text and not result.error and not blocked:
                detail = parse_detail(result.text, row.get("retailer", ""), _base_url(row.get("retailer", "")), url)
                _merge_non_empty(row, detail)
                row["fetch_method"] = _append_token(row.get("fetch_method", ""), result.method)
            else:
                detail_error = result.error or ("blocked_html" if blocked else "empty_detail")
                row["parse_status"] = _append_token(row.get("parse_status", ""), f"detail_fetch_failed:{detail_error}")
        elif not detail_done:
            row["parse_status"] = _append_token(row.get("parse_status", ""), "detail_fetch_skipped")
        review_result = _merge_magalu_reviews(row, url)
        if row.get("retailer") == "Magalu" and review_result is not None:
            if review_result.get("success"):
                magalu_review_blocked_streak = 0
            elif _has_blocked_graphql_trace(review_result):
                magalu_review_blocked_streak += 1
                _abort_on_magalu_blocked_streak(
                    "review",
                    magalu_review_blocked_streak,
                    magalu_review_abort_threshold,
                    output,
                    enriched + [row],
                )
            else:
                magalu_review_blocked_streak = 0
        _merge_magalu_pdp_html(row, url)
        _merge_casas_bahia_apis(row)
        enriched.append(row)
        method = row.get("fetch_method") or (result.method if result else "")
        print(
            f"[seda] detail {index}/{total_rows} {_safe_log_value(row.get('retailer'))} "
            f"sku={_safe_log_value(row.get('sku'))} method={_safe_log_value(method)} "
            f"status={_safe_log_value(str(row.get('parse_status', '')).split('+')[-1])}",
            flush=True,
        )
        if checkpoint_every and index % checkpoint_every == 0:
            write_csv(output, enriched, columns=OUTPUT_COLUMNS)
            print(f"[seda] checkpoint {output} rows={len(enriched)}", flush=True)
    enriched = _backfill_magalu_shipping_blanks(enriched, output, checkpoint_every=checkpoint_every)
    write_csv(output, enriched, columns=OUTPUT_COLUMNS)
    print(f"[seda] wrote {output} rows={len(enriched)}")


if __name__ == "__main__":
    main()


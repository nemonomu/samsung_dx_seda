import os
import re
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException, WebDriverException

from ..parsers import parse_detail
from ..step00_config import OUTPUT_COLUMNS, product_line, read_csv, run_root, write_csv
from ..step08_detail_enrichment import _detail_identity_mode


FIELDS = [
    "retailer_sku_name",
    "original_sku_price",
    "final_sku_price",
    "savings",
    "sku",
    "screen_size",
    "estimated_annual_electricity_use",
    "ref_capacity",
    "ldy_capacity",
    "ldy_loading_type",
    "model_year",
    "retailer_sku_name_similar",
]


def main():
    root = run_root()
    input_csv = os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_INPUT", "").strip() or _default_input(root)
    output_csv = os.getenv(
        "SEDA_CASAS_BAHIA_HTML_BACKFILL_OUTPUT",
        str(root / "output" / "final_output_enriched_html.csv"),
    )
    rows = read_csv(input_csv)
    limit = int(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_LIMIT", "0"))
    skip = int(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_SKIP", "0"))
    checkpoint_every = int(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_CHECKPOINT_EVERY", "10"))
    wait_seconds = float(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_WAIT_SECONDS", "2.0"))
    retries = int(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_RETRIES", "1"))
    raw_dir = Path(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_RAW_DIR", str(root / "detail" / "html_backfill")))
    save_raw = os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_SAVE_RAW", "0").lower() in {"1", "true", "yes", "y"}
    if save_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)

    indexes = [index for index, row in enumerate(rows) if _needs_backfill(row)]
    if skip:
        indexes = indexes[skip:]
    if limit:
        indexes = indexes[:limit]

    driver = _create_driver()
    try:
        for done, index in enumerate(indexes, start=1):
            row = rows[index]
            parsed = _fetch_detail(driver, row.get("product_url", ""), wait_seconds, retries)
            if parsed.get("_html") and save_raw:
                (raw_dir / f"{index + 1:04d}_{row.get('item') or 'item'}.html").write_text(
                    parsed.pop("_html"),
                    encoding="utf-8",
                    errors="ignore",
                )
            _merge(row, parsed)
            print(
                f"[casas] html backfill {done}/{len(indexes)} row={index + 1} "
                f"item={row.get('item','')} energy={row.get('estimated_annual_electricity_use','')}",
                flush=True,
            )
            if checkpoint_every and done % checkpoint_every == 0:
                write_csv(output_csv, rows, columns=OUTPUT_COLUMNS)
                print(f"[casas] checkpoint {output_csv} rows={len(rows)}", flush=True)
    finally:
        driver.quit()

    write_csv(output_csv, rows, columns=OUTPUT_COLUMNS)
    print(f"[casas] wrote {output_csv} rows={len(rows)} targets={len(indexes)}")


def _default_input(root):
    preferred = root / "output" / "final_output_enriched_pickup.csv"
    if preferred.exists():
        return str(preferred)
    return str(root / "output" / "final_output_enriched.csv")


def _needs_backfill(row):
    retailer = row.get("retailer") or row.get("account_name") or ""
    normalized_retailer = re.sub(r"[^a-z]", "", str(retailer).lower())
    if normalized_retailer and normalized_retailer != "casasbahia":
        return False
    line = str(row.get("product_line") or row.get("product") or product_line()).strip().upper()
    semantic_fields = {
        "TV": ("screen_size", "estimated_annual_electricity_use"),
        "REF": ("ref_capacity", "estimated_annual_electricity_use"),
        "LDY": (
            "ldy_capacity",
            "ldy_loading_type",
            "estimated_annual_electricity_use",
        ),
    }.get(line, ("estimated_annual_electricity_use",))
    return any(not row.get(field) for field in semantic_fields)


def _create_driver():
    options = uc.ChromeOptions()
    profile = os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_PROFILE", "C:/tmp/seda_network_capture_profile")
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_HEADLESS", "0").lower() in {"1", "true", "yes", "y"}:
        options.add_argument("--headless=new")
    options.page_load_strategy = os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_PAGE_LOAD_STRATEGY", "eager")
    version = os.getenv("SEDA_CAPTURE_CHROME_VERSION", "").strip()
    kwargs = {"options": options, "use_subprocess": True}
    if version:
        kwargs["version_main"] = int(version)
    return uc.Chrome(**kwargs)


def _fetch_detail(driver, url, wait_seconds, retries):
    if not url:
        return {}
    timeout = int(os.getenv("SEDA_CASAS_BAHIA_HTML_BACKFILL_PAGE_TIMEOUT", "20"))
    base_url = "https://www.casasbahia.com.br"
    for attempt in range(retries + 1):
        try:
            driver.set_page_load_timeout(timeout)
            try:
                driver.get(url)
            except TimeoutException:
                _stop_loading(driver)
            time.sleep(wait_seconds + attempt)
            html = driver.page_source or ""
            detail = parse_detail(html, "Casas Bahia", base_url, url)
            semantic_fields = {
                "TV": ("screen_size", "estimated_annual_electricity_use"),
                "REF": ("ref_capacity", "estimated_annual_electricity_use"),
                "LDY": (
                    "ldy_capacity",
                    "ldy_loading_type",
                    "estimated_annual_electricity_use",
                ),
            }.get(product_line(), ("estimated_annual_electricity_use",))
            if any(detail.get(field) for field in semantic_fields):
                detail["_html"] = html
                return detail
        except WebDriverException as exc:
            if attempt >= retries:
                return {"parse_status": f"casas_html_backfill_error:{type(exc).__name__}"}
            time.sleep(2 + attempt)
    return {}


def _stop_loading(driver):
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass


def _merge(row, detail):
    if not detail:
        return False
    identity_mode = _detail_identity_mode(row, detail)
    if identity_mode not in {'verified', 'same_name'}:
        reason = 'identity_conflict' if identity_mode == 'conflict' else 'missing_product_identity'
        row['parse_status'] = _append_token(
            row.get('parse_status', ''),
            f'casas_html_backfill_{reason}',
        )
        return False
    for field in FIELDS:
        value = detail.get(field)
        if not value:
            continue
        if field == "retailer_sku_name" and row.get(field) and not row.get(field, "").endswith("..."):
            continue
        if field in {"original_sku_price", "final_sku_price", "savings", "retailer_sku_name_similar"} and row.get(field):
            continue
        row[field] = value
    row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_html_backfill")
    row["parse_status"] = _append_token(row.get("parse_status", ""), detail.get("parse_status", "detail_casas_bahia_html"))
    return True


def _append_token(value, token):
    value = str(value or "").strip()
    token = str(token or "").strip()
    if not token:
        return value
    if not value:
        return token
    parts = value.split("+")
    return value if token in parts else f"{value}+{token}"


if __name__ == "__main__":
    main()

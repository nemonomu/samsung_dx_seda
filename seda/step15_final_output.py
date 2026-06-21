import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .parsers import (
    format_brl,
    ldy_sku_short_version_from_text,
    ref_sku_short_version_from_text,
)
from .step00_config import product_line, read_csv, run_root, write_csv


DELIMITER = " ||| "

COMMON_FINAL_COLUMNS = [
    "country",
    "product",
    "item",
    "account_name",
    "page_type",
    "retailer_sku_name",
    "product_url",
    "original_sku_price",
    "final_sku_price",
    "savings",
    "sku_status",
    "discount_type",
    "delivery_availability",
    "pick_up_availability",
]

PRODUCT_EXTRA_COLUMNS = {
    "TV": ["sku", "screen_size", "estimated_annual_electricity_use", "model_year"],
    "REF": ["ref_refrigerator_type", "ref_capacity", "sku_short_version", "sku"],
    "LDY": ["ldy_loading_type", "ldy_color", "ldy_capacity", "sku_short_version", "sku"],
}

REVIEW_FINAL_COLUMNS = [
    "summarized_review_content",
    "retailer_sku_name_similar",
    "star_rating",
    "count_of_star_ratings",
    "count_of_reviews",
    "recommendation_intent",
    "detailed_review_content",
    "bsr_rank",
    "main_rank",
    "calendar_week",
    "crawl_strdatetime",
    "batch_id",
]

FINAL_OUTPUT_COLUMNS = COMMON_FINAL_COLUMNS + PRODUCT_EXTRA_COLUMNS["TV"] + REVIEW_FINAL_COLUMNS

def final_output_columns(product_line_value=None):
    line = (product_line_value or product_line()).strip().upper()
    extras = PRODUCT_EXTRA_COLUMNS.get(line, PRODUCT_EXTRA_COLUMNS["TV"])
    return COMMON_FINAL_COLUMNS + extras + REVIEW_FINAL_COLUMNS

def _active_retailer():
    return (os.getenv("SEDA_ACTIVE_RETAILER") or os.getenv("SEDA_RETAILERS") or "").strip().lower()

def _batch_id(now):
    prefix = "m" if _active_retailer() == "magalu" else "c"
    return f"{prefix}_{now.strftime('%Y%m%d_%H%M%S')}"

def main():
    root = run_root()
    source = _source_path(root)
    rows = read_csv(source)
    now = _run_datetime()
    output_rows = [_format_row(row, now) for row in rows]
    output = Path(os.getenv("SEDA_FINAL_OUTPUT_CSV", str(root / "output" / "final_output.csv")))
    columns = final_output_columns()
    write_csv(output, output_rows, columns=columns)
    _write_manifest(root, source, output, output_rows, now)
    print(f"[seda] wrote {output} rows={len(output_rows)}")

def _source_path(root):
    override = os.getenv("SEDA_FINAL_SOURCE_CSV", "").strip()
    if override:
        return Path(override)
    badged = root / "output" / "final_output_badged.csv"
    if badged.exists():
        return badged
    enriched = root / "output" / "final_output_enriched.csv"
    if enriched.exists():
        return enriched
    current_final = root / "output" / "final_output.csv"
    if current_final.exists() and _has_internal_columns(current_final):
        return current_final
    return root / "output" / "seda_final_targets.csv"

def _has_internal_columns(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
            fieldnames = csv.DictReader(f).fieldnames or []
    except OSError:
        return False
    return "product_line" in fieldnames or "retailer" in fieldnames

def _run_datetime():
    raw = os.getenv("SEDA_CRAWL_STRDATETIME", "").strip()
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return datetime.now().replace(microsecond=0)

def _format_row(row, now):
    item = _item_from_url(row.get("product_url", ""))
    sku = _sku_for_output(row, item)
    original_sku_price = _price_for_output(row.get("original_sku_price", ""))
    final_sku_price = _price_for_output(row.get("final_sku_price", ""))
    if original_sku_price and final_sku_price and _prices_equal(original_sku_price, final_sku_price):
        original_sku_price = ""
    return {
        "country": "SEDA",
        "product": product_line(),
        "item": row.get("item") or item or sku,
        "account_name": _account_name_for_output(row),
        "page_type": _page_type(row),
        "retailer_sku_name": row.get("retailer_sku_name", ""),
        "product_url": row.get("product_url", ""),
        "original_sku_price": original_sku_price,
        "final_sku_price": final_sku_price,
        "savings": _savings_for_output(row),
        "sku_status": row.get("sku_status", ""),
        "discount_type": _discount_type_for_output(row.get("discount_type", "")),
        "delivery_availability": _delivery_for_output(row),
        "pick_up_availability": _pickup_for_output(row),
        "sku": sku,
        "screen_size": row.get("screen_size", ""),
        "estimated_annual_electricity_use": _energy_use_for_output(row),
        "model_year": row.get("model_year", ""),
        "ref_refrigerator_type": row.get("ref_refrigerator_type", ""),
        "ref_capacity": row.get("ref_capacity", ""),
        "ldy_loading_type": row.get("ldy_loading_type", ""),
        "ldy_color": row.get("ldy_color", ""),
        "ldy_capacity": row.get("ldy_capacity", ""),
        "sku_short_version": _sku_short_version_for_output(row),
        "summarized_review_content": _summary_for_output(row.get("summarized_review_content", "")),
        "retailer_sku_name_similar": _join_values(row.get("retailer_sku_name_similar", ""), filter_noise=True),
        "star_rating": _star_rating_for_output(row.get("star_rating", "")),
        "count_of_star_ratings": _count_metric_for_output(row.get("count_of_star_ratings", "")),
        "count_of_reviews": _count_metric_for_output(row.get("count_of_reviews", "")),
        "recommendation_intent": row.get("recommendation_intent", ""),
        "detailed_review_content": _join_reviews(row.get("detailed_review_content", "")),
        "bsr_rank": row.get("bsr_rank", ""),
        "main_rank": row.get("main_rank", ""),
        "calendar_week": f"w{now.isocalendar().week}",
        "crawl_strdatetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_id": _batch_id(now),
    }


def _delivery_for_output(row):
    text = str(row.get("delivery_availability") or "").strip()
    if _is_casas_bahia_row(row) and "calculo de frete apresentou problemas" in _ascii_key(text):
        return ""
    return text

def _energy_use_for_output(row):
    return str(row.get("estimated_annual_electricity_use") or "").strip()

def _sku_for_output(row, item):
    line = product_line()
    if _active_retailer() == "casas_bahia" and line in {"TV", "REF"}:
        return ""
    sku = str(row.get("sku") or "").strip()
    if not sku:
        return ""
    if item and sku == item:
        return ""
    if _is_synthetic_sku(row, sku):
        return ""
    return sku

def _is_synthetic_sku(row, sku):
    if not _is_magalu_row(row):
        return False
    text = str(sku or "").strip()
    if re.fullmatch(r"(?:110|127|220|240)\s*v(?:olts?)?|bivolt", text, re.I):
        return True
    if len(text) > 40:
        return True
    if re.search(r"\b(?:smart\s*tv|televisor|geladeira|refrigerador|maquina\s+de\s+lavar|máquina\s+de\s+lavar|lavadora)\b", text, re.I):
        return True
    return False

def _sku_short_version_for_output(row):
    line = product_line()
    if _active_retailer() == "magalu" and line in {"REF", "LDY"}:
        return ""
    if line == "REF" and _active_retailer() == "casas_bahia":
        sku = str(row.get("sku") or "").strip()
        if sku:
            return sku
    value = str(row.get("sku_short_version") or "").strip()
    if value:
        return value
    name = row.get("retailer_sku_name", "")
    if line == "REF":
        return ref_sku_short_version_from_text(name)
    if line == "LDY":
        return ldy_sku_short_version_from_text(name)
    return ""

def _price_for_output(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"R\$\d{1,3}(?:\.\d{3})*,\d{2}", text):
        return text
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return format_brl(text)
    return text

def _prices_equal(left, right):
    left_number = _price_number(left)
    right_number = _price_number(right)
    if left_number is None or right_number is None:
        return str(left or "").strip() == str(right or "").strip()
    return left_number == right_number

def _price_number(value):
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None

def _savings_for_output(row):
    if _is_magalu_row(row):
        return ""
    text = str(row.get("savings") or "").strip()
    if not text:
        return ""
    baixou = re.search(r"baixou\s+(-?\d+(?:[.,]\d+)?)%", text, re.I)
    if baixou:
        return f"Baixou {baixou.group(1).replace(',', '.')}%"
    percent = re.fullmatch(r"(\d+(?:[.,]\d+)?)%", text)
    if percent and _is_casas_bahia_row(row):
        return f"Baixou {percent.group(1).replace(',', '.')}%"
    return text

def _pickup_for_output(row):
    if _is_magalu_row(row) and product_line() != "TV":
        return ""
    return row.get("pick_up_availability", "")

def _discount_type_for_output(value):
    text = str(value or "").strip()
    if not text:
        return ""
    percent_discount = re.fullmatch(r"(\d+(?:[.,]\d+)?)%\s*(?:OFF|discount(?:\s+off)?)", text, re.I)
    if percent_discount:
        return f"{percent_discount.group(1).replace(',', '.')}% discount off"
    percent_desconto = re.fullmatch(r"(\d+(?:[.,]\d+)?)%\s*(?:de\s+)?desconto", text, re.I)
    if percent_desconto:
        return f"{percent_desconto.group(1).replace(',', '.')}% discount off"
    return text

def _is_casas_bahia_row(row):
    retailer = str(row.get("retailer") or row.get("account_name") or "").lower()
    url = str(row.get("product_url") or "").lower()
    return "casas" in retailer or "casasbahia.com.br" in url

def _is_magalu_row(row):
    retailer = str(row.get("retailer") or row.get("account_name") or "").lower()
    url = str(row.get("product_url") or "").lower()
    return "magalu" in retailer or "magazineluiza.com.br" in url

def _account_name_for_output(row):
    if _is_magalu_row(row):
        return "Magalu"
    return row.get("retailer") or row.get("account_name", "")

def _page_type(row):
    if row.get("main_rank"):
        return "main"
    if row.get("bsr_rank"):
        return "bsr"
    return row.get("page_type", "")

def _star_rating_for_output(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return "0"
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    if number == 0:
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"

def _count_metric_for_output(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return "0"
    normalized = re.sub(r"[^\d,.-]", "", text)
    if not normalized:
        return "0"
    if "," in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", normalized):
        normalized = normalized.replace(".", "")
    try:
        number = float(normalized)
    except ValueError:
        return text
    if number == 0:
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"

def _summary_for_output(value):
    text = _join_values(value)
    if _is_synthetic_review_summary(text):
        return ""
    return text

def _is_synthetic_review_summary(text):
    return bool(re.search(r"(?:^|\s\|\|\|\s)(?:Average rating|Star ratings|Comments):", str(text or ""), re.I))

def _join_reviews(value):
    values = _as_review_list(value)
    if not values:
        return ""
    if len(values) == 1 and str(values[0]).strip().lower().startswith("review1 -"):
        return str(values[0]).strip()
    return DELIMITER.join(f"review{index} - {text}" for index, text in enumerate(values, start=1))

def _join_values(value, filter_noise=False):
    values = _as_list(value)
    if filter_noise:
        values = [value for value in values if not _value_noise(value)]
    return DELIMITER.join(values)

def _value_noise(value):
    normalized = str(value or "").strip().lower()
    noise_markers = (
        "boas vindas",
        "entre ou cadastre",
        "cadastre-se",
        "digite seu cep",
        "atendimento",
    )
    return any(marker in normalized for marker in noise_markers)

def _as_list(value):
    if value in (None, ""):
        return []
    text = str(value).strip()
    if not text:
        return []
    if DELIMITER in text:
        return [part.strip() for part in text.split(DELIMITER) if part.strip()]
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    if isinstance(parsed, list):
        return [_stringify(item) for item in parsed if _stringify(item)]
    if isinstance(parsed, dict):
        return [_stringify(parsed)]
    return [_stringify(parsed)]

def _as_review_list(value):
    if value in (None, ""):
        return []
    text = str(value).strip()
    if not text:
        return []
    if DELIMITER in text:
        return [part.strip() for part in text.split(DELIMITER)]
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    if isinstance(parsed, list):
        return [_stringify_review(item) for item in parsed]
    if isinstance(parsed, dict):
        return [_stringify(parsed)]
    return [_stringify_review(parsed)]

def _stringify(value):
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        for key in ("name", "title", "description", "text", "reviewBody", "summary"):
            if value.get(key):
                return str(value[key]).strip()
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()

def _stringify_review(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return _stringify(value)
    return str(value).strip()

def _item_from_url(url):
    parts = [part for part in str(url or "").split("/") if part]
    try:
        index = parts.index("p")
    except ValueError:
        return ""
    return parts[index + 1] if len(parts) > index + 1 else ""

def _write_manifest(root, source, output, rows, now):
    main_count = sum(1 for row in rows if row.get("main_rank"))
    bsr_count = sum(1 for row in rows if row.get("bsr_rank"))
    payload = {
        "source": str(source),
        "output": str(output),
        "rows": len(rows),
        "main_rank_rows": main_count,
        "bsr_rank_rows": bsr_count,
        "calendar_week": f"w{now.isocalendar().week}",
        "crawl_strdatetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_id": _batch_id(now),
    }
    manifest_override = os.getenv("SEDA_FINAL_MANIFEST_JSON", "").strip()
    if manifest_override:
        path = Path(manifest_override)
    else:
        output_path = Path(output)
        path = output_path.with_name(f"{output_path.stem}.manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

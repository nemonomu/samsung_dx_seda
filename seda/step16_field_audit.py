import csv
import json
import re
from collections import Counter
from pathlib import Path

from .step00_config import read_csv, run_root, write_json
from .step15_final_output import final_output_columns


DELIMITER = " ||| "
FINAL_OUTPUT_COLUMNS = final_output_columns()


PARSER_CRITERIA = {
    "delivery_availability": (
        "freight response options[] where name is not Retira/pickup; "
        "save name + formattedDeadlineDelivery or deliveryDateDetailed."
    ),
    "pick_up_availability": (
        "freight response options[] where name contains Retira/pickup; "
        "save name + formattedDeadlineDelivery or deliveryDateDetailed."
    ),
    "estimated_annual_electricity_use": (
        "product source spec Consumo de energia raw text; classify units but do not infer missing unit."
    ),
    "sku_status": "listing card Patrocinado mapped to Sponsored; otherwise blank.",
    "reviews": "count fields from total review count; detailed_review_content joins non-empty review bodies with delimiter.",
    "similar": "similar product names joined with delimiter.",
    "ref_refrigerator_type": "REF type from retailer detail specs, e.g. Magalu Ficha Tecnica > Porta.",
    "ref_capacity": "REF capacity from retailer detail specs, e.g. Magalu Ficha Tecnica > Capacidade Liquida total.",
    "ldy_loading_type": (
        "Casas Bahia: canonical or alias labels, then explicit loading-direction phrases in description/title; "
        "never use unrelated spec values. Magalu: preserve the existing exact-value fallback."
    ),
    "ldy_capacity": (
        "Casas Bahia: one unambiguous exact capacity in the product title first, then retailer detail targets; "
        "Magalu: retailer detail targets first. Preserve target ranges/approximate values when no reliable exact "
        "Casas Bahia title capacity exists."
    ),
    "sku_short_version": "Short model code from retailer_sku_name, e.g. RS58 or RF29D.",
}


def main():
    root = run_root()
    final_output = root / "output" / "final_output.csv"
    enriched = root / "output" / "final_output_enriched.csv"
    rows = read_csv(final_output)
    enriched_rows = read_csv(enriched)

    report, suspicious = audit_rows(final_output, rows, enriched_rows)
    output_dir = root / "output"
    write_json(output_dir / "field_audit_v2.json", report)
    _write_suspicious(output_dir / "field_audit_v2_suspicious_rows.csv", suspicious)
    print(
        "[seda] wrote field_audit_v2.json "
        f"rows={len(rows)} anomalies={sum(item['count'] for item in report['anomalies'])}"
    )


def audit_rows(final_output, rows, enriched_rows=None):
    enriched_rows = enriched_rows or []
    columns = _read_columns(final_output)
    report = {
        "final_output": str(final_output),
        "rows": len(rows),
        "columns": columns,
        "expected_columns": FINAL_OUTPUT_COLUMNS,
        "missing_columns": [column for column in FINAL_OUTPUT_COLUMNS if column not in columns],
        "extra_columns": [column for column in columns if column not in FINAL_OUTPUT_COLUMNS],
        "parser_criteria": PARSER_CRITERIA,
        "field_stats": _field_stats(rows),
        "energy_class_counts": (
            dict(Counter(_energy_class(row.get("estimated_annual_electricity_use", "")) for row in rows))
            if "estimated_annual_electricity_use" in columns
            else {}
        ),
        "fetch_method_counts_enriched": dict(Counter(row.get("fetch_method", "") for row in enriched_rows if row.get("fetch_method", ""))),
        "parse_status_counts_enriched": dict(Counter(row.get("parse_status", "") for row in enriched_rows if row.get("parse_status", ""))),
        "anomalies": [],
    }
    suspicious = []

    _add_schema_anomalies(report, suspicious)
    _add_field_anomalies(report, suspicious, rows, enriched_rows)
    report["anomaly_counts"] = {item["name"]: item["count"] for item in report["anomalies"]}
    return report, suspicious


def _read_columns(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return csv.DictReader(f).fieldnames or []


def _field_stats(rows):
    stats = {}
    for column in FINAL_OUTPUT_COLUMNS:
        values = [str(row.get(column, "") or "").strip() for row in rows]
        nonempty = [value for value in values if value]
        stats[column] = {"nonempty": len(nonempty), "unique": len(set(nonempty))}
    return stats


def _add_schema_anomalies(report, suspicious):
    for name, severity, columns in (
        ("missing_columns", "critical", report["missing_columns"]),
        ("extra_columns", "low", report["extra_columns"]),
    ):
        if not columns:
            continue
        note = "Columns do not match final output contract."
        report["anomalies"].append({"name": name, "severity": severity, "count": len(columns), "note": note, "samples": columns})
        suspicious.append({"anomaly": name, "severity": severity, "row": "", "item": "", "field": ",".join(columns), "value": ""})


def _add_field_anomalies(report, suspicious, rows, enriched_rows):
    enriched_by_key = {}
    for row in enriched_rows:
        for key in _row_keys(row):
            enriched_by_key.setdefault(key, row)

    checks = [
        ("delivery_availability_blank", "critical", _is_blank_delivery, "delivery_availability"),
        ("invalid_sku_status", "high", _is_invalid_sku_status, "sku_status"),
        ("invalid_price_format", "high", _has_invalid_price, "original_sku_price,final_sku_price"),
        ("review_comments_gt_star_ratings", "high", _has_review_comments_gt_star_ratings, "count_of_star_ratings,count_of_reviews"),
        ("invalid_star_rating", "medium", _has_invalid_star_rating, "star_rating"),
        ("empty_sku", "medium", _has_empty_sku, "sku"),
        ("similar_missing_delimiter", "low", _similar_missing_delimiter, "retailer_sku_name_similar"),
        ("review_missing_delimiter", "low", _review_missing_delimiter, "detailed_review_content"),
        ("energy_value_other", "low", lambda row: _energy_class(row.get("estimated_annual_electricity_use", "")) == "other", "estimated_annual_electricity_use"),
        ("energy_unit_missing", "info", lambda row: _energy_class(row.get("estimated_annual_electricity_use", "")) == "number_only", "estimated_annual_electricity_use"),
        ("single_review_body_but_total_count_gt_one", "info", _single_review_body_but_total_count_gt_one, "detailed_review_content"),
    ]

    for name, severity, predicate, field in checks:
        hits = []
        for index, row in enumerate(rows, start=1):
            try:
                matched = predicate(row)
            except Exception:
                matched = True
            if matched:
                enriched = next((enriched_by_key.get(key) for key in _row_keys(row) if enriched_by_key.get(key)), None)
                hit = _sample_row(index, row, field, enriched)
                hits.append(hit)
                suspicious.append({"anomaly": name, "severity": severity, **hit})
        if hits:
            report["anomalies"].append(
                {
                    "name": name,
                    "severity": severity,
                    "count": len(hits),
                    "note": _note_for(name),
                    "samples": hits[:20],
                }
            )


def _sample_row(index, row, field, enriched):
    return {
        "row": index,
        "item": row.get("item", ""),
        "product_url": row.get("product_url", ""),
        "retailer_sku_name": row.get("retailer_sku_name", ""),
        "sku": row.get("sku", ""),
        "field": field,
        "value": _field_value(row, field),
        "parse_status": (enriched or {}).get("parse_status", ""),
        "fetch_method": (enriched or {}).get("fetch_method", ""),
    }


def _row_keys(row):
    keys = []
    for value in (row.get("item", ""), _item_from_url(row.get("product_url", "")), row.get("product_url", "")):
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


def _item_from_url(url):
    parts = [part for part in str(url or "").split("/") if part]
    try:
        index = parts.index("p")
    except ValueError:
        return ""
    return parts[index + 1] if len(parts) > index + 1 else ""


def _field_value(row, field):
    values = []
    for column in field.split(","):
        column = column.strip()
        values.append(f"{column}={row.get(column, '')}")
    return " | ".join(values)


def _is_blank_delivery(row):
    return not str(row.get("delivery_availability", "") or "").strip()


def _is_invalid_sku_status(row):
    value = str(row.get("sku_status", "") or "").strip()
    return value not in {"", "Sponsored"}


def _has_invalid_price(row):
    for column in ("original_sku_price", "final_sku_price"):
        value = str(row.get(column, "") or "").strip()
        if value and not re.fullmatch(r"R\$\d{1,3}(?:\.\d{3})*,\d{2}", value):
            return True
    return False


def _has_review_comments_gt_star_ratings(row):
    left = str(row.get("count_of_star_ratings", "") or "").strip()
    right = str(row.get("count_of_reviews", "") or "").strip()
    if not left or not right:
        return False
    return _int_text(right) > _int_text(left)


def _has_invalid_star_rating(row):
    value = str(row.get("star_rating", "") or "").strip().replace(",", ".")
    if not value:
        return False
    if not re.fullmatch(r"\d(?:\.\d{1,2})?", value):
        return True
    number = float(value)
    return number < 0 or number > 5


def _has_empty_sku(row):
    return not str(row.get("sku", "") or "").strip()


def _similar_missing_delimiter(row):
    value = str(row.get("retailer_sku_name_similar", "") or "").strip()
    return bool(value) and DELIMITER not in value and _looks_like_multi_value(value)


def _review_missing_delimiter(row):
    value = str(row.get("detailed_review_content", "") or "").strip()
    if not value:
        return False
    return "review2 -" in value and DELIMITER not in value


def _single_review_body_but_total_count_gt_one(row):
    value = str(row.get("detailed_review_content", "") or "").strip()
    if not value or DELIMITER in value:
        return False
    total = _int_text(row.get("count_of_reviews", ""))
    return total > 1 and value.lower().startswith("review1 -")


def _looks_like_multi_value(value):
    return value.count(DELIMITER) > 0


def _int_text(value):
    digits = re.sub(r"\D+", "", str(value or ""))
    return int(digits) if digits else 0


def _energy_class(value):
    text = str(value or "").strip()
    if not text:
        return "blank"
    normalized = text.lower()
    if "kwh" in normalized:
        return "kWh"
    if re.search(r"(?:^|[\d\s,.;(<])w(?:$|[\s).;,])", normalized) or re.search(r"\bwatts?\b", normalized):
        return "W"
    if re.fullmatch(r"\d+(?:[,.]\d+)?", normalized):
        return "number_only"
    return "other"


def _note_for(name):
    return {
        "delivery_availability_blank": (
            "Source is confirmed as freight options[]. Blank means freight collection/access failed, not parser uncertainty."
        ),
        "invalid_sku_status": "sku_status must be blank or Sponsored.",
        "invalid_price_format": "Price should be formatted as R$9.999,99.",
        "review_comments_gt_star_ratings": "count_of_reviews is comments; it should not be greater than count_of_star_ratings.",
        "invalid_star_rating": "star_rating should be a number from 0 to 5.",
        "empty_sku": "sku is model number per ERD; keep blank if model number is unavailable.",
        "similar_missing_delimiter": "Multiple similar names should use the configured delimiter.",
        "review_missing_delimiter": "Multiple review bodies should use the configured delimiter.",
        "energy_value_other": "Energy source is raw Consumo de energia text; check unusual text but do not infer missing unit.",
        "energy_unit_missing": "Energy value is numeric only; source unit is missing or not exposed.",
        "single_review_body_but_total_count_gt_one": "Review API can expose one non-empty body while total count is greater than one.",
    }.get(name, "")


def _write_suspicious(path, rows):
    columns = [
        "anomaly",
        "severity",
        "row",
        "item",
        "product_url",
        "retailer_sku_name",
        "sku",
        "field",
        "value",
        "parse_status",
        "fetch_method",
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()

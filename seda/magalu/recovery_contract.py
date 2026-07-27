"""Magalu recovery-field contracts for each recovery source.

The paid ZenRows field fallback stays limited to the original seven semantic
fields.  The read-only last-known DB fallback also covers SKU and the LDY
colour field, whose validation rules live in ``last_known_db``.
"""


MAGALU_RECOVERY_FIELD_MAP = {
    "TV": (
        "screen_size",
        "estimated_annual_electricity_use",
        "model_year",
    ),
    "REF": (
        "ref_refrigerator_type",
        "ref_capacity",
    ),
    "LDY": (
        "ldy_loading_type",
        "ldy_capacity",
    ),
}


MAGALU_LAST_KNOWN_DB_FIELD_MAP = {
    "TV": (
        "sku",
        "screen_size",
        "estimated_annual_electricity_use",
        "model_year",
    ),
    "REF": (
        "sku",
        "ref_refrigerator_type",
        "ref_capacity",
    ),
    "LDY": (
        "sku",
        "ldy_loading_type",
        "ldy_color",
        "ldy_capacity",
    ),
}


def recovery_fields(product_line_value):
    line = str(product_line_value or "").strip().upper()
    return MAGALU_RECOVERY_FIELD_MAP.get(line, ())


def last_known_fields(product_line_value):
    line = str(product_line_value or "").strip().upper()
    return MAGALU_LAST_KNOWN_DB_FIELD_MAP.get(line, ())

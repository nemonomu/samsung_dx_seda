"""Casas Bahia recovery-field contracts, separate from Magalu."""


CASAS_ZENROWS_FIELD_MAP = {
    "TV": (
        "sku",
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
        "ldy_color",
        "ldy_capacity",
    ),
}


CASAS_LAST_KNOWN_DB_FIELD_MAP = {
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
        "ldy_loading_type",
        "ldy_color",
        "ldy_capacity",
    ),
}


def zenrows_fields(product_line_value):
    line = str(product_line_value or "").strip().upper()
    return CASAS_ZENROWS_FIELD_MAP.get(line, ())


def last_known_fields(product_line_value):
    line = str(product_line_value or "").strip().upper()
    return CASAS_LAST_KNOWN_DB_FIELD_MAP.get(line, ())

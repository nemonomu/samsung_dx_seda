import re
import unicodedata


TRANSLATABLE_FIELDS = {
    "sku_status",
    "discount_type",
    "delivery_availability",
    "pick_up_availability",
    "recommendation_intent",
    "ref_refrigerator_type",
    "ldy_loading_type",
}


WEEKDAY_TRANSLATIONS = {
    "segunda": "Monday",
    "segunda-feira": "Monday",
    "terca": "Tuesday",
    "terca-feira": "Tuesday",
    "quarta": "Wednesday",
    "quarta-feira": "Wednesday",
    "quinta": "Thursday",
    "quinta-feira": "Thursday",
    "sexta": "Friday",
    "sexta-feira": "Friday",
    "sabado": "Saturday",
    "domingo": "Sunday",
}


MONTH_TRANSLATIONS = {
    "janeiro": "January",
    "fevereiro": "February",
    "marco": "March",
    "abril": "April",
    "maio": "May",
    "junho": "June",
    "julho": "July",
    "agosto": "August",
    "setembro": "September",
    "outubro": "October",
    "novembro": "November",
    "dezembro": "December",
}


def translate_row(row):
    translated = dict(row)
    for field in TRANSLATABLE_FIELDS:
        if field in translated:
            translated[field] = translate_value(field, translated.get(field, ""))
    return translated


def translate_value(field, value):
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    parts = [part.strip() for part in text.split(";")]
    translated_parts = [_translate_single(field, part) for part in parts if part]
    return "; ".join(dict.fromkeys(part for part in translated_parts if part))


def _translate_single(field, text):
    if field == "sku_status":
        return _translate_sku_status(text)
    if field == "discount_type":
        return _translate_discount_type(text)
    if field == "delivery_availability":
        return _translate_delivery(text)
    if field == "pick_up_availability":
        return _translate_pickup(text)
    if field == "recommendation_intent":
        return _translate_recommendation(text)
    if field == "ref_refrigerator_type":
        return _translate_ref_refrigerator_type(text)
    if field == "ldy_loading_type":
        return _translate_ldy_loading_type(text)
    return text


def _translate_sku_status(text):
    if re.search(r"patrocinado|sponsored", _normalize(text), re.I):
        return "Sponsored"
    return text


def _translate_discount_type(text):
    normalized = _normalize(text)
    coupon_value = re.search(r"cupom\s+r\$\s*([\d.,]+)\s*off", normalized, re.I)
    if coupon_value:
        return f"Coupon R$ {coupon_value.group(1)} off"
    coupon_code = re.search(r"use?\s+o\s+cupom\s+(.+)", text, re.I)
    if coupon_code:
        return f"Use coupon {coupon_code.group(1).strip()}"
    percent = re.search(r"(\d+(?:[.,]\d+)?)%\s+(?:de\s+)?desconto", normalized, re.I)
    if percent:
        return f"{percent.group(1).replace(',', '.')}% discount off"
    percent_off = re.search(r"(\d+(?:[.,]\d+)?)%\s*off", normalized, re.I)
    if percent_off:
        return f"{percent_off.group(1).replace(',', '.')}% discount off"
    percent_discount = re.search(r"(\d+(?:[.,]\d+)?)%\s*discount(?:\s+off)?", normalized, re.I)
    if percent_discount:
        return f"{percent_discount.group(1).replace(',', '.')}% discount off"
    if re.search(r"desconto.*carne|carne.*desconto", normalized, re.I):
        return "Discount with digital payment booklet"
    if "desconto" in normalized:
        return _replace_discount_word(text)
    return text


def _translate_delivery(text):
    normalized = _normalize(text)
    if normalized == "receba em casa":
        return "Receive at home"
    if re.search(r"\breceba\s+hoje\b", normalized, re.I):
        return "Receive today"
    if re.search(r"\breceba\s+amanh", normalized, re.I):
        return "Receive tomorrow"
    if normalized == "entrega rapida":
        return "Fast delivery"
    if normalized == "frete gratis":
        return "Free shipping"
    scheduled = re.search(r"agendada\s+a\s+partir\s+de\s+(.+)", normalized, re.I)
    if scheduled:
        return f"Scheduled from {_translate_date_text(scheduled.group(1).strip())}"
    if "entrega indisponivel para este cep" in normalized:
        return "Delivery unavailable for this ZIP code"
    if "entrega indisponivel" in normalized or "produto indisponivel" in normalized:
        return "Delivery unavailable for your region at the moment. Try another ZIP code?"
    express = re.search(r"expressa\s+ate\s+(.+)", normalized, re.I)
    if express:
        return f"Express delivery by {_translate_date_text(express.group(1).strip())}"
    economy = re.search(r"economica\s+ate\s+(.+)", normalized, re.I)
    if economy:
        return f"Economy delivery by {_translate_date_text(economy.group(1).strip())}"
    normal = re.search(r"normal\s+ate\s+(.+)", normalized, re.I)
    if normal:
        return f"Normal by {_translate_date_text(normal.group(1).strip())}"
    days = re.search(r"receba\s+em\s+ate\s+(\d+)\s+dias?\s+ut(?:il|eis)", normalized, re.I)
    if days:
        unit = "business day" if days.group(1) == "1" else "business days"
        return f"Receive within {days.group(1)} {unit}"
    receive_by = re.search(r"receba\s+ate\s+(.+)", normalized, re.I)
    if receive_by:
        return f"Receive by {_translate_date_text(receive_by.group(1).strip())}"
    if normalized.startswith("receba"):
        return re.sub(r"^receba", "Receive", text, flags=re.I)
    return text


def _translate_pickup(text):
    normalized = _normalize(text)
    fast = re.search(r"retira\s+rapido\s+retirar\s+em\s+(.+)", normalized, re.I)
    if fast:
        return f"Fast pickup in {fast.group(1).strip()}"
    if re.search(r"retire.*amanh", normalized, re.I):
        return "Pick up in store starting tomorrow"
    if re.search(r"retire|retira", normalized, re.I):
        return "Pick up in store"
    return text


def _translate_recommendation(text):
    match = re.search(r"(\d+(?:[.,]\d+)?)%\s+dos\s+clientes\s+recomendam", _normalize(text), re.I)
    if match:
        return f"{_integer_percent(match.group(1))}% recommend this product"
    return text


def _integer_percent(value):
    try:
        return str(int(float(str(value).replace(",", "."))))
    except ValueError:
        return str(value)


def _translate_ref_refrigerator_type(text):
    normalized = _normalize(text)
    if not normalized:
        return ""
    if normalized in {"sim", "nao", "1", "2", "02", "3", "4"}:
        return ""
    if re.search(r"\b\d+(?:[,.]\d+)?\s*(?:cm|mm|m)\b", normalized, re.I):
        return ""
    if re.search(r"\b1\s*porta\b|uma\s+porta|porta unica", normalized, re.I):
        return "Single Door"
    if re.search(r"\b2\s*portas\b|duas\s+portas", normalized, re.I):
        return "Two Door"
    if re.search(r"\b3\s*portas\b|tres\s+portas", normalized, re.I):
        return "Three Door"
    if re.search(r"\b4\s*portas\b|quatro\s+portas", normalized, re.I):
        return "Four Door"
    if "porta francesa" in normalized or normalized.startswith("french"):
        return "French Door"
    if "side by side" in normalized:
        return "Side by Side"
    if "multidoor" in normalized or "multi door" in normalized:
        return "Multidoor"
    if "top freezer" in normalized or normalized.startswith("duplex"):
        return "Freezer-on-Top"
    if normalized.startswith("inverse") or normalized.startswith("inverso"):
        return "Freezer-on-Bottom"
    if normalized in {"inverter", "aco", "inox look", "flat", "reversivel"}:
        return ""
    if re.search(r"\b(chapa|metalica|pintada|black inox)\b", normalized, re.I):
        return ""
    return text

def _translate_ldy_loading_type(text):
    normalized = _normalize(text)
    if re.search(r"superior|top\s*load|carga\s+superior", normalized, re.I):
        return "Top load"
    if re.search(r"frontal|front\s*load", normalized, re.I):
        return "Front load"
    return text


def translate_weekday(value):
    key = _normalize(value)
    return WEEKDAY_TRANSLATIONS.get(key, value)


def _translate_date_text(value):
    normalized = _normalize(value)
    weekday_first = re.search(r"([a-z-]+),\s*(\d{1,2})\s+de\s+([a-z]+)", normalized, re.I)
    if weekday_first:
        weekday = WEEKDAY_TRANSLATIONS.get(weekday_first.group(1), weekday_first.group(1))
        month = MONTH_TRANSLATIONS.get(weekday_first.group(3), weekday_first.group(3))
        return f"{weekday}, {month} {weekday_first.group(2)}"
    weekday_last = re.search(r"(\d{1,2})\s+de\s+([a-z]+),\s*([a-z-]+)", normalized, re.I)
    if weekday_last:
        weekday = WEEKDAY_TRANSLATIONS.get(weekday_last.group(3), weekday_last.group(3))
        month = MONTH_TRANSLATIONS.get(weekday_last.group(2), weekday_last.group(2))
        return f"{weekday}, {month} {weekday_last.group(1)}"
    parts = [part.strip() for part in normalized.split(",")]
    if parts:
        parts[0] = WEEKDAY_TRANSLATIONS.get(parts[0], parts[0])
    translated = ", ".join(parts)
    for source, target in MONTH_TRANSLATIONS.items():
        translated = re.sub(rf"\b{re.escape(source)}\b", target, translated, flags=re.I)
    translated = re.sub(r"\bde\s+", "", translated, flags=re.I)
    return translated


def _replace_discount_word(text):
    return re.sub(r"desconto", "discount", text, flags=re.I)


def _normalize(value):
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())

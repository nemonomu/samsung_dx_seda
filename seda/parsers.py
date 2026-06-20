import html
import json
import os
import re
import unicodedata
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from .step00_config import DEFAULT_COUNTRY, normalized_product_url, product_line


try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


BRL_RE = re.compile(r"R\$\s*[\d\.]+,\d{2}")


def clean_text(value):
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    text = text.replace("Ąą", '"')
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def compact_json(value):
    if value in ("", None, [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def absolute_url(base_url, href):
    if not href:
        return ""
    return urljoin(base_url, href)


def sku_from_url(url):
    parsed = urlparse(url)
    match = re.search(r"/p/([^/]+)(?:/|$)", parsed.path)
    if match:
        return match.group(1)
    match = re.search(r"(?:skuId|produto|productId)=([^&]+)", parsed.query)
    return match.group(1) if match else ""


def screen_size_from_text(text):
    text = clean_text(text)
    match = re.search(r"(\d{2,3})\s*(?:\"|''|polegadas|pol\b|in\b)", text, re.I)
    if not match:
        match = re.search(
            r"\b(?:smart\s+)?tv\b[^\d]{0,50}(\d{2,3})\b\s*(?=(?:4k|8k|full\s*hd|hd|uhd|qled|oled|led|crystal|neo)\b)",
            text,
            re.I,
        )
    if not match:
        match = re.search(
            r"\b(?:qled|oled|crystal|uhd|led)\b[^\d]{0,25}(\d{2,3})\b\s*(?=(?:4k|8k|full\s*hd|hd)\b)",
            text,
            re.I,
        )
    return f'{match.group(1)}"' if match else ""


def model_number_from_text(text):
    text = clean_text(text).upper()
    excludes = {
        "4K",
        "8K",
        "HD",
        "FHD",
        "UHD",
        "QLED",
        "OLED",
        "LED",
        "DLED",
        "HDR",
        "HDR10",
        "HDR10+",
        "DOLBY",
        "ATMOS",
        "VISION",
        "GOOGLE",
        "ROKU",
        "TIZEN",
        "VIDAA",
        "WEBOS",
        "ALEXA",
        "WIFI",
        "WI",
        "FI",
        "USB",
        "HDMI",
        "60HZ",
        "120HZ",
        "144HZ",
        "2025",
        "2026",
    }
    hyphenated = re.findall(
        r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]{2,}(?:-[A-Z0-9]{1,6})+\b",
        text,
    )
    plain = re.findall(r"\b(?=[A-Z0-9/]*[A-Z])(?=[A-Z0-9/]*\d)[A-Z0-9]+(?:/[A-Z0-9]+)?\b", text)
    candidates = hyphenated + plain
    for candidate in candidates:
        compact = candidate.replace("/", "").replace("-", "")
        if candidate in excludes or compact in excludes:
            continue
        if re.fullmatch(r"TV-?\d{2,3}", candidate):
            continue
        if len(compact) < 4 and not re.fullmatch(r"[A-Z]{1,3}\d[A-Z0-9]?", compact):
            continue
        if re.fullmatch(r"\d+(?:K|HZ|HDMI|USB)", compact):
            continue
        if re.fullmatch(r"20[1-3]\d", compact):
            continue
        return candidate
    return ""


def model_year_from_text(text):
    years = [int(item) for item in re.findall(r"\b(20[1-3]\d)\b", text)]
    return str(max(years)) if years else ""


def ref_sku_short_version_from_text(text):
    match = re.search(r"\b((?:RS|RF|RT|RB|RL|RR)\d{2}[A-Z]?)", str(text or "").upper())
    return match.group(1) if match else ""


def ldy_sku_short_version_from_text(text):
    match = re.search(r"\b((?:WW|WD|WF|WA)\d{2}[A-Z]{1,2})", str(text or "").upper())
    return match.group(1) if match else ""


def ldy_sku_from_text(text):
    match = re.search(r"\b((?:WW|WD|WF|WA)\d{2}[A-Z0-9]{4,12})\b", str(text or "").upper())
    return match.group(1) if match else ""


def appliance_model_number_from_text(text):
    text = clean_text(text).upper()
    candidates = re.findall(
        r"\b(?=[A-Z0-9/-]*[A-Z])(?=[A-Z0-9/-]*\d)[A-Z0-9]+(?:[-/][A-Z0-9]+)*\b",
        text,
    )
    for candidate in candidates:
        compact = re.sub(r"[\s._/-]+", "", candidate)
        if len(compact) < 4:
            continue
        if re.fullmatch(r"\d+(?:[,.]\d+)?(?:KG|KGS|L|LITROS?|V|VOLTS?)", compact):
            continue
        if re.fullmatch(r"(?:110|127|220|240)V", compact):
            continue
        if re.fullmatch(r"\d+(?:K|HZ|HDMI|USB)", compact):
            continue
        if compact in {"BIVOLT", "INVERTER", "FROSTFREE", "FROSTFREEINVERTER"}:
            continue
        return candidate
    return ""


def ldy_color_from_text(text):
    match = re.search(
        r"\b(Inox|Black|Branca|Branco|Preta|Preto|Prata|Cinza|Grafite|Titanium|Tit[aâ]nio)\b",
        str(text or ""),
        re.I,
    )
    return clean_text(match.group(1)) if match else ""


def extract_jsonld(html_text):
    blocks = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.I | re.S,
    ):
        raw = html.unescape(match.group(1).strip())
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            blocks.extend(parsed)
        else:
            blocks.append(parsed)
    return blocks


def extract_next_data(html_text):
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html_text, re.S | re.I)
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except ValueError:
        return {}


def _price_fields(text):
    prices = BRL_RE.findall(text)
    original = prices[0] if prices else ""
    final = prices[-1] if prices else ""
    pix = re.search(r"(?:ou|preço)?\s*(R\$\s*[\d\.]+,\d{2})\s*no\s*pix", text, re.I)
    if pix:
        final = pix.group(1)
    return original, final


def _rating_fields(text):
    match = re.search(r"\b([1-5][,.]\d)\s*\(([\d\.]+)\)", text)
    if match:
        return match.group(1).replace(",", "."), match.group(2)
    return "", ""


def _discount_text(text):
    phrases = []
    for pattern in [
        r"Cupom\s+R\$\s*[\d\.]+(?:,\d{2})?\s*OFF",
        r"Use\s+o\s+cupom\s+[A-Za-z0-9_-]+",
        r"\d+(?:[.,]\d+)?%\s+de\s+desconto\s+no\s+pix",
        r"\d+(?:[.,]\d+)?%\s+(?:de\s+)?desconto",
    ]:
        phrases.extend(re.findall(pattern, text, re.I))
    return "; ".join(dict.fromkeys(clean_text(item) for item in phrases))


def parse_listing(html_text, retailer, base_url, source_url, run_id="main"):
    if retailer == "Magalu":
        next_rows = _parse_magalu_next_listing(html_text, base_url, source_url, run_id)
        if next_rows:
            return next_rows
    if retailer == "Casas Bahia":
        next_rows = _parse_casas_bahia_ssr_listing(html_text, base_url, source_url, run_id)
        if next_rows:
            return next_rows
        if _casas_bahia_allow_showcase_fallback():
            next_rows = _parse_casas_bahia_next_listing(html_text, base_url, source_url, run_id)
            if next_rows:
                return next_rows

    rows = []
    seen = set()
    if BeautifulSoup:
        soup = BeautifulSoup(html_text, "html.parser")
        anchors = soup.select("a[href]")
        for anchor in anchors:
            href = anchor.get("href") or ""
            url = absolute_url(base_url, href)
            if not _looks_like_product_url(url, retailer):
                continue
            text = clean_text(anchor.get("aria-label") or anchor.get_text(" "))
            if len(text) < 12:
                parent = anchor
                for _ in range(3):
                    parent = parent.parent if parent else None
                    if parent is None:
                        break
                    text = clean_text(parent.get_text(" "))
                    if BRL_RE.search(text):
                        break
            key = sku_from_url(url) or url
            if key in seen:
                continue
            seen.add(key)
            rows.append(_listing_row(retailer, text, url, source_url, run_id, len(rows) + 1))

    if not rows:
        rows.extend(_parse_listing_from_jsonld(html_text, retailer, base_url, source_url, run_id))
    return rows


def _parse_magalu_next_listing(html_text, base_url, source_url, run_id):
    data = extract_next_data(html_text)
    products = (
        data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
        .get("search", {})
        .get("products", [])
    )
    if not isinstance(products, list):
        return []
    rows = []
    for product in products:
        if not isinstance(product, dict):
            continue
        if not _magalu_is_relevant_product(product):
            continue
        rows.append(_magalu_product_row(product, base_url, source_url, run_id, len(rows) + 1))
    return rows


MAGALU_TV_SUBCATEGORIES = {"TVES", "TV4K", "ELIT", "TLED", "TVIA"}
MAGALU_TV_PATH_RE = re.compile(r"/et/(?:tves|tv4k|elit|tled|tvia)(?:/|$)", re.I)
MAGALU_ACCESSORY_PATH_RE = re.compile(r"/et/(?:suar|sufx|cttv|easc|racm|rcpt|eace)(?:/|$)", re.I)
MAGALU_ACCESSORY_TITLE_RE = re.compile(
    r"^\s*(?:suporte|controle\s+remoto|rack|painel|receptor|moldura|fixador|tubo\s+fixo|base|cabo|pedestal)\b"
    r"|\b(?:suporte\s+(?:tv|para\s+tv|de\s+tv)|controle\s+remoto|rack\s+para\s+tv|painel\s+para\s+tv|"
    r"moldura\s+samsung|fixador\s+suporte|base\s+para\s+tv)\b",
    re.I,
)
MAGALU_STRONG_TV_TITLE_RE = re.compile(
    r"^\s*(?:smart\s+tv|tv|televisor)\b|\b(?:smart\s+tv|google\s+tv|roku\s+tv|qled|oled|nanocell|crystal\s+uhd)\b",
    re.I,
)
MAGALU_REF_EXCLUDE_RE = re.compile(
    r"\b(?:filtro|refil|prateleira|gaveta|puxador|borracha|termostato|organizador|adesivo|capa)\b",
    re.I,
)
MAGALU_REF_TITLE_RE = re.compile(
    r"\b(?:geladeira|refrigerador(?:a)?|freezer|frigobar|side\s+by\s+side|frost\s+free|duplex)\b",
    re.I,
)
MAGALU_LDY_EXCLUDE_RE = re.compile(
    r"\b(?:tanquinho|centrifuga|centr[ií]fuga|lava\s+jato|pe[çc]a|mangueira|filtro|suporte|capa|kit)\b",
    re.I,
)
MAGALU_LDY_TITLE_RE = re.compile(
    r"\b(?:lavadora|lava\s+e\s+seca|secadora|m[aá]quina\s+de\s+lavar|maquina\s+de\s+lavar|washer)\b",
    re.I,
)


def _magalu_is_relevant_product(product):
    line = product_line()
    if line == "REF":
        return _magalu_is_ref_product(product)
    if line == "LDY":
        return _magalu_is_ldy_product(product)
    return _magalu_is_tv_product(product)


def _magalu_is_tv_product(product):
    title = clean_text(product.get("title") or product.get("name"))
    path = product.get("path") or ""
    subcategory = product.get("subcategory") if isinstance(product.get("subcategory"), dict) else {}
    category_id = clean_text(subcategory.get("id")).upper()
    has_tv_category = category_id in MAGALU_TV_SUBCATEGORIES or bool(MAGALU_TV_PATH_RE.search(path))
    strong_tv_title = bool(MAGALU_STRONG_TV_TITLE_RE.search(title)) or bool(screen_size_from_text(title) and re.search(r"\b(?:tv|smart|led|qled|oled|uhd|4k|8k|full\s*hd|google\s+tv|roku)\b", title, re.I))
    accessory_title = bool(MAGALU_ACCESSORY_TITLE_RE.search(title))
    accessory_path = bool(MAGALU_ACCESSORY_PATH_RE.search(path))
    if accessory_title:
        return False
    if has_tv_category:
        return True
    if accessory_path and not strong_tv_title:
        return False
    return strong_tv_title


def _magalu_is_ref_product(product):
    title = clean_text(product.get("title") or product.get("name"))
    path = clean_text(product.get("path"))
    haystack = f"{title} {path}"
    if MAGALU_REF_EXCLUDE_RE.search(haystack):
        return False
    return bool(MAGALU_REF_TITLE_RE.search(haystack))


def _magalu_is_ldy_product(product):
    title = clean_text(product.get("title") or product.get("name"))
    path = clean_text(product.get("path"))
    haystack = f"{title} {path}"
    if MAGALU_LDY_EXCLUDE_RE.search(haystack):
        return False
    return bool(MAGALU_LDY_TITLE_RE.search(haystack))


def _magalu_product_row(product, base_url, source_url, run_id, rank):
    now = datetime.now().isoformat(timespec="seconds")
    seller = product.get("seller") if isinstance(product.get("seller"), dict) else {}
    price = product.get("price") if isinstance(product.get("price"), dict) else {}
    rating = product.get("rating") if isinstance(product.get("rating"), dict) else {}
    shipping = product.get("shippingTag") if isinstance(product.get("shippingTag"), dict) else {}
    tags = seller.get("tags") if isinstance(seller.get("tags"), list) else []
    path = product.get("path") or ""
    seller_id = seller.get("id") or _seller_id_from_url(path) or _seller_id_from_url(product.get("url") or "")
    product_url = absolute_url(base_url, path)
    if seller_id and "seller_id=" not in product_url:
        joiner = "&" if "?" in product_url else "?"
        product_url = f"{product_url}{joiner}seller_id={seller_id}"
    item_id = sku_from_url(product_url) or clean_text(product.get("id"))

    return {
        "retailer": "Magalu",
        "country": DEFAULT_COUNTRY,
        "product_line": product_line(),
        "category": "Retail.com",
        "main_rank": "" if run_id == "bsr" else rank,
        "bsr_rank": rank if run_id == "bsr" else "",
        "product_url": product_url,
        "item": item_id,
        "retailer_sku_name": clean_text(product.get("title")),
        "original_sku_price": format_brl(price.get("fullPrice")),
        "final_sku_price": format_brl(price.get("bestPrice") or price.get("price")),
        "savings": "",
        "sku_status": magalu_sku_status(product),
        "discount_type": magalu_coupon_text(tags),
        "delivery_availability": clean_text(shipping.get("time")),
        "pick_up_availability": clean_text(shipping.get("complement")),
        "sku": item_id or seller.get("sku") or "",
        "screen_size": screen_size_from_text(product.get("title") or ""),
        "model_year": model_year_from_text(product.get("title") or ""),
        "star_rating": "",
        "count_of_star_ratings": "",
        "count_of_reviews": "",
        "source_url": source_url,
        "crawl_datetime": now,
        "fetch_method": "next_data",
        "parse_status": "listing_next_data",
        "seller_id": clean_text(seller_id),
    }


def _seller_id_from_url(url):
    if not url:
        return ""
    parsed = urlparse(str(url))
    values = parse_qs(parsed.query).get("seller_id") or []
    return clean_text(values[0]) if values else ""


def _parse_casas_bahia_next_listing(html_text, base_url, source_url, run_id):
    data = extract_next_data(html_text)
    products = (
        data.get("props", {})
        .get("pageProps", {})
        .get("data", {})
        .get("casasBahiaSearch", {})
        .get("products", [])
    )
    if not isinstance(products, list):
        return []
    rows = []
    for product in products:
        if not isinstance(product, dict):
            continue
        if not _casas_bahia_is_relevant_product(product):
            continue
        rows.append(_casas_bahia_product_row(product, base_url, source_url, run_id, len(rows) + 1))
    return rows


def _casas_bahia_allow_showcase_fallback():
    return os.getenv("SEDA_CASAS_BAHIA_SHOWCASE_FALLBACK", "0").lower() in {"1", "true", "yes", "y"}


def _parse_casas_bahia_ssr_listing(html_text, base_url, source_url, run_id):
    data = extract_next_data(html_text)
    search = data.get("props", {}).get("pageProps", {}).get("initialState", {}).get("search", {})
    products = search.get("results", {}).get("products", [])
    if not isinstance(products, list):
        return []
    snapshots = _casas_bahia_card_snapshots(html_text, base_url)
    tv_only = product_line() == "TV" and _casas_bahia_tv_listing(source_url, search)
    rows = []
    for product in products:
        if not isinstance(product, dict):
            continue
        if tv_only and not _casas_bahia_is_tv_product(product):
            continue
        if not tv_only and not _casas_bahia_is_relevant_product(product):
            continue
        row = _casas_bahia_ssr_product_row(product, base_url, source_url, run_id, len(rows) + 1)
        snapshot = snapshots.get(normalized_product_url(row.get("product_url", ""))) or {}
        for key, value in snapshot.items():
            if value and not row.get(key):
                row[key] = value
        rows.append(row)
    return rows


def _casas_bahia_ssr_product_row(product, base_url, source_url, run_id, rank):
    now = datetime.now().isoformat(timespec="seconds")
    product_url = absolute_url(base_url, product.get("href") or product.get("url") or "")
    price = product.get("price") if isinstance(product.get("price"), dict) else {}
    rating = clean_text(product.get("rating") if product.get("rating") not in (None, "") else product.get("reviews"))
    rating_count = clean_text(
        product.get("ratingCount")
        if product.get("ratingCount") not in (None, "")
        else product.get("reviewsCount")
        if product.get("reviewsCount") not in (None, "")
        else product.get("ratingComments")
    )
    flags = product.get("flags") if isinstance(product.get("flags"), list) else []
    seals = product.get("seals") if isinstance(product.get("seals"), list) else []
    sku_status = _casas_bahia_sku_status(product)
    title = product.get("title") or product.get("name")
    old_price = _first_value(price, ["oldPrice", "priceFrom"]) or product.get("oldPrice")
    current_price = _first_value(price, ["currentPrice", "price", "bestPrice"]) or product.get("price")
    discount_rate = product.get("discountRate") or _first_value(price, ["discountRate", "discount"])
    discount_description = clean_text(price.get("discountDescription") or product.get("priceDescription"))

    return {
        "retailer": "Casas Bahia",
        "country": DEFAULT_COUNTRY,
        "product_line": product_line(),
        "category": "Retail.com",
        "main_rank": "" if run_id == "bsr" else rank,
        "bsr_rank": rank if run_id == "bsr" else "",
        "product_url": product_url,
        "retailer_sku_name": clean_text(title),
        "original_sku_price": format_brl(old_price),
        "final_sku_price": format_brl(current_price),
        "savings": _casas_bahia_savings_text(price, discount_rate),
        "sku_status": sku_status,
        "discount_type": _casas_bahia_ssr_discount_text(price, flags, seals),
        "delivery_availability": "",
        "pick_up_availability": _casas_bahia_availability_pickup_text(price) or _casas_bahia_pickup_text(flags),
        "sku": _casas_bahia_listing_sku(title or "")
        or ("" if product_line() in {"REF", "LDY"} else clean_text(product.get("idSku") or product.get("sku")) or sku_from_url(product_url)),
        "screen_size": screen_size_from_text(title or ""),
        "model_year": model_year_from_text(title or ""),
        "star_rating": _zero_preserving_metric(rating),
        "count_of_star_ratings": _zero_preserving_metric(rating_count),
        "count_of_reviews": _zero_preserving_metric(rating_count),
        "source_url": source_url,
        "crawl_datetime": now,
        "fetch_method": "casas_bahia_ssr_next_data",
        "parse_status": "listing_casas_bahia_partner_api" if product.get("name") else "listing_casas_bahia_ssr",
        "retailer_product_id": clean_text(product.get("id")),
        "seller_id": clean_text(product.get("lojista") or product.get("sellerId")),
    }


def _casas_bahia_availability_pickup_text(price):
    availability = price.get("availability") if isinstance(price.get("availability"), dict) else {}
    return "Retirada disponivel" if availability.get("Retira") else ""


def _casas_bahia_listing_sku(title):
    line = product_line()
    if line in {"REF", "LDY"}:
        return appliance_model_number_from_text(title or "")
    return model_number_from_text(title or "")


def _casas_bahia_sku_status(product):
    if product.get("isSponsored") or product.get("advertasingEvents") or product.get("advertisingEvents"):
        return "Sponsored"
    tag = clean_text(product.get("tagName"))
    return "Sponsored" if re.search(r"patrocinado|sponsored", tag, re.I) else ""


def _casas_bahia_product_row(product, base_url, source_url, run_id, rank):
    now = datetime.now().isoformat(timespec="seconds")
    product_url = absolute_url(base_url, product.get("url") or "")
    rating = clean_text(product.get("reviews"))
    reviews_count = clean_text(product.get("reviewsCount"))
    discount_rate = product.get("discountRate")
    flags = product.get("flags") if isinstance(product.get("flags"), list) else []
    stamp = product.get("stamp") if isinstance(product.get("stamp"), dict) else {}

    return {
        "retailer": "Casas Bahia",
        "country": DEFAULT_COUNTRY,
        "product_line": product_line(),
        "category": "Retail.com",
        "main_rank": "" if run_id == "bsr" else rank,
        "bsr_rank": rank if run_id == "bsr" else "",
        "product_url": product_url,
        "retailer_sku_name": clean_text(product.get("title")),
        "original_sku_price": format_brl(product.get("oldPrice")),
        "final_sku_price": format_brl(product.get("price")),
        "savings": _baixou_text(discount_rate),
        "sku_status": _casas_bahia_sku_status(product),
        "discount_type": _casas_bahia_discount_text(product, flags, stamp),
        "delivery_availability": "",
        "pick_up_availability": _casas_bahia_pickup_text(flags),
        "sku": _casas_bahia_listing_sku(product.get("title") or "")
        or ("" if product_line() in {"REF", "LDY"} else clean_text(product.get("sku")) or sku_from_url(product_url) or clean_text(product.get("id"))),
        "screen_size": screen_size_from_text(product.get("title") or ""),
        "model_year": model_year_from_text(product.get("title") or ""),
        "star_rating": _zero_preserving_metric(rating),
        "count_of_star_ratings": _zero_preserving_metric(reviews_count),
        "count_of_reviews": _zero_preserving_metric(reviews_count),
        "source_url": source_url,
        "crawl_datetime": now,
        "fetch_method": "casas_bahia_showcase",
        "parse_status": "listing_casas_bahia_showcase",
        "retailer_product_id": clean_text(product.get("id")),
        "seller_id": clean_text(product.get("sellerId")),
    }


def _casas_bahia_tv_listing(source_url, search):
    query = search.get("query") if isinstance(search.get("query"), dict) else {}
    term = clean_text(search.get("searchTerm") or query.get("strbusca"))
    return "/tv/" in str(source_url).lower() or term.lower() == "tv"


def _casas_bahia_is_relevant_product(product):
    line = product_line()
    if line == "TV":
        return _casas_bahia_is_tv_product(product)
    if line == "REF":
        return _casas_bahia_is_ref_product(product)
    if line == "LDY":
        return _casas_bahia_is_ldy_product(product)
    return True


def _casas_bahia_is_ref_product(product):
    text = _casas_bahia_product_haystack(product)
    if _matches_any(
        text,
        [
            r"\bfiltro\b",
            r"\brefil\b",
            r"\bprateleira\b",
            r"\bgaveta\b",
            r"\bpuxador\b",
            r"\bborracha\b",
            r"\btermostato\b",
            r"\borganizador\b",
        ],
    ):
        return False
    return _matches_any(
        text,
        [
            r"\bgeladeira\b",
            r"\brefrigerador(?:a)?\b",
            r"\bfreezer\b",
            r"\bfrigobar\b",
            r"\bside\s+by\s+side\b",
            r"\bfrost\s+free\b",
            r"\bduplex\b",
        ],
    )


def _casas_bahia_is_ldy_product(product):
    text = _casas_bahia_product_haystack(product)
    if _matches_any(
        text,
        [
            r"\blava[\s-]+loucas\b",
            r"\bmangueira\b",
            r"\bfiltro\b",
            r"\bsuporte\b",
            r"\bcapa\b",
            r"\bsabao\b",
        ],
    ):
        return False
    return _matches_any(
        text,
        [
            r"\bmaquina\s+de\s+lavar\b",
            r"\blavadora\b",
            r"\blava\s+e\s+seca\b",
            r"\bsecadora\b",
            r"\btanquinho\b",
        ],
    )


def _casas_bahia_product_haystack(product):
    parts = [clean_text(product.get("title") or product.get("name"))]
    stack = []
    categories = product.get("categories") if isinstance(product.get("categories"), list) else []
    stack.extend(categories)
    for key in ("department", "category", "subcategory", "subCategory"):
        value = product.get(key)
        if isinstance(value, dict):
            stack.append(value)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            parts.append(value)
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        parts.append(clean_text(item.get("name") or item.get("title") or item.get("description")))
        for key in ("category", "subcategory", "subCategory", "children"):
            child = item.get(key)
            if isinstance(child, dict):
                stack.append(child)
            elif isinstance(child, list):
                stack.extend(child)
    return _normalize_key(" ".join(part for part in parts if part))


def _matches_any(text, patterns):
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _casas_bahia_is_tv_product(product):
    title = clean_text(product.get("title") or product.get("name"))
    if _casas_bahia_excluded_non_tv_title(title):
        return False
    if re.search(r"\b(?:smart\s*)?tv\b|televisor", title, re.I):
        return True
    categories = product.get("categories") if isinstance(product.get("categories"), list) else []
    stack = list(categories)
    for key in ("department", "category", "subcategory"):
        if isinstance(product.get(key), dict):
            stack.append(product[key])
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        if re.search(r"\bTV\b|televis", name, re.I):
            return True
        children = item.get("subCategory")
        if isinstance(children, list):
            stack.extend(children)
        for key in ("category", "subcategory", "subCategory"):
            child = item.get(key)
            if isinstance(child, dict):
                stack.append(child)
            elif isinstance(child, list):
                stack.extend(child)
    return False


def _casas_bahia_excluded_non_tv_title(title):
    text = _normalize_key(title)
    if re.search(r"\bpainel\b", text, re.I) and not re.search(r"\bsmart\s+tv\b|televisor", text, re.I):
        return True
    patterns = [
        r"\bsuporte\b",
        r"\brack\b",
        r"\bpainel\s+home\b",
        r"\bhome\s+suspenso\b",
        r"\bcomod[ao]\b",
        r"\bc[oô]moda\b",
        r"\binstala\s*tv\b",
        r"\bservico\s+de\s+instalacao\b",
        r"\bmesa\b",
        r"\bestante\b",
    ]
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _casas_bahia_card_snapshots(html_text, base_url):
    if not BeautifulSoup:
        return {}
    soup = BeautifulSoup(html_text, "html.parser")
    snapshots = {}
    cards = soup.select('[data-testid="product-card-item"], [data-testid="product-card-desktop"]')
    for card in cards:
        anchor = card.select_one('a[href*="/p/"]')
        if not anchor:
            continue
        product_url = absolute_url(base_url, anchor.get("href"))
        key = normalized_product_url(product_url)
        if not key or key in snapshots:
            continue
        text = clean_text(card.get_text(" "))
        original, final = _casas_bahia_dom_prices(card, text)
        snapshots[key] = {
            "original_sku_price": original,
            "final_sku_price": final,
            "savings": _savings_from_text(text),
            "discount_type": _discount_text(text),
            "pick_up_availability": _first_phrase(text, ["Retira", "Retire"]),
        }
    return snapshots


def _casas_bahia_dom_prices(node, text):
    history = _node_text(node.select_one('[data-testid="history-price"]')) if hasattr(node, "select_one") else ""
    original = BRL_RE.search(history).group(0) if BRL_RE.search(history) else ""
    price_nodes = node.select('[data-testid*="price"]') if hasattr(node, "select") else []
    price_text = " ".join(
        _node_text(item)
        for item in price_nodes
        if "skeleton" not in " ".join(item.get("class", [])).lower()
    )
    prices = BRL_RE.findall(price_text) or BRL_RE.findall(text)
    final = prices[-1] if prices else ""
    if original and final == original and len(prices) > 1:
        final = prices[-1]
    if not original and len(prices) > 1:
        original = prices[0]
    return original, final


def _node_text(node):
    return clean_text(node.get_text(" ")) if node else ""


def _casas_bahia_dom_discount(text):
    values = []
    if re.search(r"\bpix\b", text, re.I):
        values.append("No Pix")
    if re.search(r"carn[eê]", text, re.I):
        values.append("Carne Digital")
    return "; ".join(values)


def _first_value(value, keys):
    if not isinstance(value, dict):
        return ""
    for key in keys:
        if value.get(key) not in (None, ""):
            return value.get(key)
    return ""


def _casas_bahia_ssr_discount_text(price, flags, seals):
    values = []
    description = clean_text(_first_value(price, ["priceDescription", "paymentMethod", "label"]))
    if description and re.search(r"cupom|desconto", description, re.I):
        values.append(description)
    for collection in (flags, seals):
        for item in collection:
            if not isinstance(item, dict):
                continue
            text = clean_text(item.get("description") or item.get("title") or item.get("name"))
            if text and re.search(r"desconto|oferta|cupom|pix|carne|carn", text, re.I):
                values.append(text)
    return "; ".join(dict.fromkeys(values))


def _percent_text(value):
    if value in (None, "", "0", 0):
        return ""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return clean_text(value)
    return f"{number:g}%"


def _baixou_text(value):
    percent = _percent_text(value)
    return f"Baixou {percent}" if percent else ""


def _zero_preserving_metric(value):
    text = clean_text(value)
    if not text:
        return ""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    if number == 0:
        return "0"
    return f"{number:g}"


def _casas_bahia_savings_text(price, fallback_rate=""):
    explicit = clean_text(price.get("savings") if isinstance(price, dict) else "")
    if explicit:
        return explicit
    if isinstance(price, dict):
        old_price = _first_value(price, ["oldPrice", "priceFrom"])
        standard_price = _first_value(price, ["standardPrice", "price", "bestPrice"])
        computed = _discount_percent_from_prices(old_price, standard_price)
        if computed:
            return _baixou_text(computed)
    return _baixou_text(fallback_rate)


def _discount_percent_from_prices(old_price, final_price):
    try:
        old_number = float(str(old_price).replace(",", "."))
        final_number = float(str(final_price).replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if old_number <= 0 or final_number <= 0 or final_number >= old_number:
        return ""
    return round((old_number - final_number) / old_number * 100)


def _casas_bahia_discount_text(product, flags, stamp):
    values = []
    price_description = clean_text(product.get("priceDescription"))
    if price_description and re.search(r"cupom|desconto", price_description, re.I):
        values.append(price_description)
    for flag in flags:
        if not isinstance(flag, dict):
            continue
        description = clean_text(flag.get("description"))
        if description and re.search(r"desconto|oferta|cupom|pix|carne|carn", description, re.I):
            values.append(description)
    stamp_description = clean_text(stamp.get("description"))
    if stamp_description and re.search(r"desconto|oferta|cupom|pix|carne|carn", stamp_description, re.I):
        values.append(stamp_description)
    return "; ".join(dict.fromkeys(values))


def _casas_bahia_pickup_text(flags):
    for flag in flags:
        if not isinstance(flag, dict):
            continue
        description = clean_text(flag.get("description"))
        if re.search(r"retira", description, re.I):
            return "Retira Rapido"
    return ""


def format_brl(value):
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return str(value)
    whole, cents = f"{number:.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return f"R${'.'.join(reversed(groups))},{cents}"


def _magalu_savings(price):
    discount = price.get("discount") if isinstance(price, dict) else ""
    if discount in (None, ""):
        return ""
    try:
        number = float(str(discount).replace(",", "."))
    except ValueError:
        return str(discount)
    return f"{number:g}%"


def magalu_sku_status(product):
    ads = product.get("ads") if isinstance(product, dict) else {}
    ads = ads if isinstance(ads, dict) else {}
    label = clean_text(ads.get("label"))
    if ads.get("sponsored") is True:
        return label or "Patrocinado"
    if re.search(r"patrocinado|sponsored", label, re.I):
        return label
    if product.get("adsSellerId"):
        return "Patrocinado"
    return ""


def magalu_discount_text(tags, price=None):
    values = []
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            tag_type = clean_text(tag.get("type")).lower()
            value = tag.get("discountValue")
            message = clean_text(tag.get("message"))
            if tag_type == "coupon":
                if value not in (None, ""):
                    try:
                        amount = float(str(value).replace(",", "."))
                        amount_text = str(int(amount)) if amount.is_integer() else str(amount).replace(".", ",")
                        values.append(f"Cupom R$ {amount_text} OFF")
                        continue
                    except ValueError:
                        pass
                if message:
                    values.append(message)
                continue
            if value not in (None, ""):
                try:
                    amount = float(str(value).replace(",", "."))
                    amount_text = str(int(amount)) if amount.is_integer() else f"{amount:g}".replace(".", ",")
                    values.append(f"{amount_text}% OFF")
                    continue
                except ValueError:
                    pass
            if message:
                values.append(message)
    discount = price.get("discount") if isinstance(price, dict) else ""
    if discount not in (None, "", "0", "0.00"):
        try:
            amount = float(str(discount).replace(",", "."))
        except ValueError:
            amount = None
        if amount:
            amount_text = str(int(amount)) if amount.is_integer() else f"{amount:g}".replace(".", ",")
            values.append(f"{amount_text}% OFF")
    return "; ".join(dict.fromkeys(values))


def magalu_coupon_text(tags):
    if not isinstance(tags, list):
        return ""
    values = []
    for tag in tags:
        if not isinstance(tag, dict) or tag.get("type") != "coupon":
            continue
        value = tag.get("discountValue")
        if value not in (None, ""):
            try:
                amount = float(str(value).replace(",", "."))
                amount_text = str(int(amount)) if amount.is_integer() else str(amount).replace(".", ",")
                values.append(f"Cupom R$ {amount_text} OFF")
                continue
            except ValueError:
                pass
        message = clean_text(tag.get("message"))
        if message:
            values.append(message)
    return "; ".join(dict.fromkeys(values))


def _looks_like_product_url(url, retailer):
    if retailer == "Magalu":
        return "/p/" in url and "magazineluiza.com.br" in url
    if retailer == "Casas Bahia":
        return ("produto" in url.lower() or "/p/" in url.lower()) and "casasbahia.com.br" in url
    return False


def _listing_row(retailer, text, url, source_url, run_id, rank):
    original, final = _price_fields(text)
    rating, rating_count = _rating_fields(text)
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "retailer": retailer,
        "country": DEFAULT_COUNTRY,
        "product_line": product_line(),
        "category": "Retail.com",
        "main_rank": "" if run_id == "bsr" else rank,
        "bsr_rank": rank if run_id == "bsr" else "",
        "product_url": url,
        "retailer_sku_name": _name_from_listing_text(text),
        "original_sku_price": original,
        "final_sku_price": final,
        "savings": _savings_from_text(text),
        "sku_status": "Sponsored" if re.search(r"patrocinado|sponsored", text, re.I) else "",
        "discount_type": _discount_text(text) if retailer != "Magalu" else _coupon_text_from_text(text),
        "sku": sku_from_url(url),
        "screen_size": screen_size_from_text(text),
        "model_year": model_year_from_text(text),
        "star_rating": "",
        "count_of_star_ratings": "",
        "source_url": source_url,
        "crawl_datetime": now,
        "parse_status": "listing",
    }


def _name_from_listing_text(text):
    text = re.sub(r"\bFull\b\s*", "", clean_text(text), flags=re.I)
    price_pos = text.find("R$")
    if price_pos > 0:
        text = text[:price_pos]
    text = re.sub(r"\b[1-5][,.]\d\s*\([\d.]+\)\s*$", "", text).strip()
    return text


def _savings_from_text(text):
    baixou = re.search(r"Baixou\s+(-?\d+(?:[.,]\d+)?)%", text, re.I)
    if baixou:
        return f"Baixou {baixou.group(1).replace(',', '.')}%"
    match = re.search(r"(?:(\d+(?:[.,]\d+)?)%\s+OFF|(\d+(?:[.,]\d+)?)%\s+de\s+desconto)", text, re.I)
    if not match:
        return ""
    value = next(group for group in match.groups() if group)
    return f"{value.replace(',', '.')}%"


def _coupon_text_from_text(text):
    values = re.findall(r"Cupom\s+R\$\s*[\d\.]+(?:,\d{2})?\s*OFF", clean_text(text), re.I)
    return "; ".join(dict.fromkeys(clean_text(value) for value in values))


def _parse_listing_from_jsonld(html_text, retailer, base_url, source_url, run_id):
    rows = []
    for block in extract_jsonld(html_text):
        products = []
        if isinstance(block, dict) and block.get("@type") == "ItemList":
            for item in block.get("itemListElement", []):
                product = item.get("item", item) if isinstance(item, dict) else {}
                if isinstance(product, dict):
                    products.append(product)
        elif isinstance(block, dict) and block.get("@type") == "Product":
            products.append(block)
        for product in products:
            url = absolute_url(base_url, product.get("url", ""))
            if not url:
                continue
            rank = len(rows) + 1
            name = clean_text(product.get("name"))
            row = _listing_row(retailer, name, url, source_url, run_id, rank)
            offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
            if offers.get("price"):
                row["final_sku_price"] = str(offers.get("price"))
            rows.append(row)
    return rows


def parse_detail(html_text, retailer, base_url, product_url):
    text = _visible_text(html_text)
    row = {
        "retailer": retailer,
        "delivery_availability": _first_phrase(text, ["Receba", "Entrega", "Frete"]),
        "pick_up_availability": _first_phrase(text, ["Retire", "Retira", "Pickup"]),
        "sku": sku_from_url(product_url) or _match_after(text, "Código"),
        "screen_size": _spec_value(text, ["Polegadas", "Tamanho da tela"]) or screen_size_from_text(text),
        "estimated_annual_electricity_use": _spec_value(text, ["Consumo", "Eficiência energética", "Energia"]),
        "model_year": model_year_from_text(text),
        "summarized_review_content": _summary_review_content(html_text),
        "recommendation_intent": _recommendation(text),
        "detailed_review_content": compact_json(_reviews(text, limit=20)),
        "retailer_sku_name_similar": compact_json(_similar_names(html_text, base_url)),
        "parse_status": "detail",
    }
    if retailer == "Magalu":
        magalu_detail = _parse_magalu_next_detail(html_text, base_url, product_url)
        row.update({key: value for key, value in magalu_detail.items() if value not in ("", None, [], {})})
    if retailer == "Casas Bahia":
        row["delivery_availability"] = ""
        row["pick_up_availability"] = ""
        row["estimated_annual_electricity_use"] = ""
        casas_detail = _parse_casas_bahia_html_detail(html_text, base_url, product_url)
        row.update({key: value for key, value in casas_detail.items() if value not in ("", None, [], {})})
    _merge_jsonld_detail(row, html_text)
    if not row.get("count_of_reviews"):
        comments = re.search(r"([\d\.]+)\s+comentários", text, re.I)
        row["count_of_reviews"] = comments.group(1) if comments else ""
    return row


def _parse_casas_bahia_html_detail(html_text, base_url, product_url):
    description = _meta_content(html_text, "og:description") or _meta_content(html_text, "description")
    product_name = _jsonld_product_value(html_text, "name")
    title = product_name or _meta_content(html_text, "og:title") or _meta_content(html_text, "title")
    description_text = _html_break_text(description)
    specs = _casas_bahia_next_specs(html_text)
    for key, value in _casas_bahia_specs(description_text).items():
        specs.setdefault(key, value)
    model = specs.get("modelo", "")
    screen_size = (
        specs.get("tamanho da tela", "")
        or _casas_bahia_detail_label_value(html_text, ["Tamanho da tela"])
        or screen_size_from_text(description_text)
    )
    screen_size = _screen_size_value(screen_size)
    if screen_size and screen_size.isdigit():
        screen_size = f'{screen_size}"'
    energy_use = _first_spec_value(
        specs,
        ["consumo aproximado de energia", "consumo de energia"],
    ) or _casas_bahia_detail_label_value(html_text, ["Consumo de energia"])
    energy_use = _energy_use_value(energy_use)
    original, final = _casas_bahia_detail_prices(html_text)
    return {
        "retailer": "Casas Bahia",
        "retailer_sku_name": clean_text(product_name),
        "original_sku_price": original,
        "final_sku_price": final,
        "savings": _savings_from_text(_visible_text(html_text)),
        "sku": model,
        "screen_size": screen_size,
        "estimated_annual_electricity_use": energy_use,
        "model_year": _first_spec_value(specs, ["ano", "ano de lancamento"])
        or model_year_from_text(f"{title} {description_text}"),
        "summarized_review_content": "",
        "retailer_sku_name_similar": compact_json(_similar_names(html_text, base_url)),
        "parse_status": "detail_casas_bahia_html",
    }


def _casas_bahia_detail_label_value(html_text, labels):
    if not BeautifulSoup:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    wanted = [_normalize_key(label) for label in labels]
    for node in soup.find_all(string=True):
        label_text = clean_text(node)
        normalized = _normalize_key(label_text)
        if not label_text or not any(label == normalized or label in normalized for label in wanted):
            continue
        for candidate in _nearby_text_candidates(node):
            value = _value_after_label(candidate, label_text)
            if value and _normalize_key(value) != normalized:
                return value
    return ""


def _nearby_text_candidates(node):
    parent = getattr(node, "parent", None)
    if not parent:
        return []
    candidates = [clean_text(parent.get_text(" "))]
    for sibling in list(parent.find_next_siblings(limit=3)):
        candidates.append(clean_text(sibling.get_text(" ")))
    grandparent = getattr(parent, "parent", None)
    if grandparent:
        candidates.append(clean_text(grandparent.get_text(" ")))
        for sibling in list(grandparent.find_next_siblings(limit=2)):
            candidates.append(clean_text(sibling.get_text(" ")))
    return [candidate for candidate in candidates if candidate]


def _value_after_label(text, label):
    text = clean_text(text)
    label = clean_text(label)
    if not text or not label:
        return ""
    if text == label:
        return ""
    index = text.lower().find(label.lower())
    if index >= 0:
        tail = clean_text(text[index + len(label) :].lstrip(" :-"))
        if tail:
            return _trim_spec_tail(tail)
    return _trim_spec_tail(text)


def _trim_spec_tail(text):
    text = clean_text(text)
    if not text:
        return ""
    markers = [
        "Marca",
        "Modelo",
        "Resolucao",
        "Resolução",
        "Frequencia",
        "Frequência",
        "Sistema",
        "Peso",
        "Dimensoes",
        "Dimensões",
        "Garantia",
        "Voltagem",
        "Cor",
    ]
    for marker in markers:
        match = re.search(rf"\s+{re.escape(marker)}\b", text, re.I)
        if match:
            text = text[: match.start()]
            break
    return clean_text(text)


def _jsonld_product_value(html_text, key):
    for block in extract_jsonld(html_text):
        if isinstance(block, dict) and block.get("@type") == "Product":
            return clean_text(block.get(key))
    return ""


def _casas_bahia_detail_prices(html_text):
    if not BeautifulSoup:
        return "", ""
    soup = BeautifulSoup(html_text, "html.parser")
    history = _node_text(soup.select_one('[data-testid="history-price"]'))
    original_match = BRL_RE.search(history)
    original = original_match.group(0) if original_match else ""
    price_box = soup.select_one('[data-testid="product-price-value"]') or soup.select_one(
        '[data-testid="product-price-box"]'
    )
    prices = BRL_RE.findall(_node_text(price_box))
    final = prices[0] if prices else ""
    return original, final


def _meta_content(html_text, name):
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']',
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\'](.*?)["\']',
        rf'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']{re.escape(name)}["\']',
        rf'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']{re.escape(name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return html.unescape(match.group(1))
    return ""


def _html_break_text(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [clean_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _casas_bahia_specs(text):
    specs = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = _normalize_key(key)
        value = clean_text(value)
        if key and value:
            specs[key] = value
    return specs


def _casas_bahia_next_specs(html_text):
    specs = {}
    data = extract_next_data(html_text)
    for group in _iter_casas_bahia_spec_groups(data):
        items = group.get("specs") if isinstance(group, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = _normalize_key(item.get("name"))
            value = clean_text(item.get("value"))
            if key and value:
                specs.setdefault(key, value)
    return specs


def _iter_casas_bahia_spec_groups(value):
    if isinstance(value, dict):
        groups = value.get("specGroups")
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict):
                    yield group
        for child in value.values():
            yield from _iter_casas_bahia_spec_groups(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_casas_bahia_spec_groups(item)


def _screen_size_value(value):
    text = clean_text(value)
    if not text:
        return ""
    extracted = screen_size_from_text(text)
    if extracted:
        return extracted
    match = re.search(r"\b(\d{2,3})\b", text)
    return f'{match.group(1)}"' if match else text


def _energy_use_value(value):
    text = clean_text(value)
    if not text:
        return ""
    match = re.search(r"\b(\d+(?:[,.]\d+)?\s*(?:kwh\s*/?\s*m\S+|kwh|w))\b", text, re.I)
    return clean_text(match.group(1)) if match else _trim_spec_tail(text)


def _first_spec_value(specs, labels):
    for label in labels:
        wanted = _normalize_key(label)
        for key, value in specs.items():
            if wanted == key or wanted in key:
                return value
    return ""


def _summary_review_content(html_text):
    if not BeautifulSoup:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    node = soup.select_one('[data-testid="summary-detail-description"]')
    if not node:
        return ""
    return clean_text(node.get_text(" "))


def _parse_magalu_next_detail(html_text, base_url, product_url):
    data = extract_next_data(html_text)
    page_data = data.get("props", {}).get("pageProps", {}).get("data", {})
    item = page_data.get("item") if isinstance(page_data.get("item"), dict) else {}
    if not item:
        return {}

    product_rating = page_data.get("productRating") if isinstance(page_data.get("productRating"), dict) else {}
    review_summary = page_data.get("reviewSummaryQuery") if isinstance(page_data.get("reviewSummaryQuery"), dict) else {}
    general = product_rating.get("general") if isinstance(product_rating.get("general"), dict) else {}
    offer = _magalu_first_offer(item)
    best_price = offer.get("bestPrice") if isinstance(offer.get("bestPrice"), dict) else {}

    html_summary = _summary_review_content(html_text)
    line = product_line()
    model = _magalu_factsheet_value(item, ["modelo"])
    reference = _magalu_factsheet_value(item, ["referencia", "referência"])
    detail = {
        "retailer": "Magalu",
        "sku": _magalu_sku_for_product_line(line, reference, model, item, product_url),
        "retailer_sku_name": clean_text(item.get("title")),
        "original_sku_price": format_brl(offer.get("listPrice")),
        "final_sku_price": format_brl(best_price.get("totalAmount") or offer.get("price")),
        "screen_size": _magalu_attribute_value(item, ["polegadas", "tamanho da tela"])
        or _magalu_factsheet_value(item, ["polegadas", "tamanho da tela"])
        or screen_size_from_text(item.get("title") or ""),
        "estimated_annual_electricity_use": _magalu_factsheet_value(
            item,
            ["consumo aproximado de energia", "consumo", "energia"],
        ),
        "model_year": _magalu_factsheet_value(item, ["ano de lancamento", "ano"])
        or model_year_from_text(f"{item.get('title', '')} {item.get('description', '')}"),
        "summarized_review_content": html_summary or clean_text(review_summary.get("summary")),
        "retailer_sku_name_similar": compact_json(_similar_names(html_text, base_url)),
        "star_rating": clean_text(general.get("rating")),
        "count_of_star_ratings": clean_text(general.get("reviewCount")),
        "count_of_reviews": clean_text(general.get("commentCount") or general.get("reviewCount")),
        "detailed_review_content": compact_json(_magalu_review_descriptions(product_rating, limit=20)),
        "parse_status": "detail_next_data",
    }
    if line == "REF":
        detail.update(
            {
                "ref_refrigerator_type": _magalu_factsheet_value(item, ["porta"]),
                "ref_capacity": _magalu_factsheet_value(
                    item,
                    ["capacidade liquida total", "capacidade líquida total", "capacidade total"],
                ),
            }
        )
    if line == "LDY":
        detail.update(
            {
                "ldy_loading_type": _magalu_factsheet_value(
                    item,
                    [
                        "tipo de abertura",
                        "tipo de abertura eletrodomestico",
                        "tipo de abertura eletrodoméstico",
                        "tipo de abertura electrodomestico",
                    ],
                ),
                "ldy_capacity": _magalu_factsheet_value(item, ["capacidade de lavagem"]),
            }
        )
    return detail


def _magalu_sku_for_product_line(line, reference, model, item, product_url):
    fallback = clean_text(item.get("offerId") or item.get("id")) or sku_from_url(product_url)
    if line in {"REF", "LDY"}:
        return reference or model or fallback
    return model or fallback


def _magalu_first_offer(item):
    offers = item.get("offers") if isinstance(item.get("offers"), list) else []
    return offers[0] if offers and isinstance(offers[0], dict) else {}


def _magalu_attribute_value(item, labels):
    wanted = {_normalize_key(label) for label in labels}
    for attribute in item.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        label = _normalize_key(attribute.get("label") or attribute.get("type"))
        if label in wanted:
            return clean_text(attribute.get("current"))
    return ""


def _magalu_factsheet_value(item, labels):
    wanted = {_normalize_key(label) for label in labels}
    for fact in _iter_magalu_facts(item.get("factsheet") or []):
        key = _normalize_key(fact.get("keyName") or fact.get("slug"))
        if key in wanted or any(label and label in key for label in wanted):
            return _magalu_fact_value(fact)
    return ""


def _iter_magalu_facts(facts):
    if not isinstance(facts, list):
        return
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if fact.get("keyName") or fact.get("slug"):
            yield fact
        children = fact.get("elements")
        if isinstance(children, list):
            yield from _iter_magalu_facts(children)


def _magalu_fact_value(fact):
    elements = fact.get("elements")
    if isinstance(elements, list):
        values = [clean_text(element.get("value")) for element in elements if isinstance(element, dict)]
        values = [value for value in values if value]
        if values:
            return "; ".join(values)
    return clean_text(fact.get("value"))



def _magalu_review_descriptions(product_rating, limit=20):
    user_reviews = product_rating.get("userReviews") if isinstance(product_rating.get("userReviews"), dict) else {}
    reviews = []
    for item in user_reviews.get("items") or []:
        if not isinstance(item, dict):
            continue
        description = clean_text(item.get("description"))
        if description:
            reviews.append(description)
        if len(reviews) >= limit:
            break
    return reviews


def _normalize_key(value):
    normalized = unicodedata.normalize("NFKD", clean_text(value))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def _visible_text(html_text):
    if BeautifulSoup:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return clean_text(soup.get_text(" "))
    return clean_text(re.sub(r"<[^>]+>", " ", html_text))


def _first_phrase(text, starts):
    for start in starts:
        match = re.search(rf"({re.escape(start)}[^.。|]{{0,120}})", text, re.I)
        if match:
            return clean_text(match.group(1))
    return ""


def _match_after(text, label):
    match = re.search(rf"{re.escape(label)}\s*[:#]?\s*([A-Za-z0-9._-]+)", text, re.I)
    return match.group(1) if match else ""


def _spec_value(text, labels):
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*:?\s*([^|]{{1,80}})", text, re.I)
        if match:
            return clean_text(match.group(1))
    return ""


def _recommendation(text):
    match = re.search(r"(\d+%)\s+dos\s+clientes\s+recomendam", text, re.I)
    return match.group(1) if match else ""


def _reviews(text, limit=20):
    marker = re.search(r"Avaliações dos clientes|Comentários|Reviews", text, re.I)
    if not marker:
        return []
    tail = text[marker.end() :]
    candidates = []
    for part in re.split(r"(?:há\s+(?:cerca\s+de\s+)?\d+\s+\w+|Ver mais|Denunciar)", tail, flags=re.I):
        part = clean_text(part)
        if 25 <= len(part) <= 500 and not BRL_RE.search(part):
            candidates.append(part)
        if len(candidates) >= limit:
            break
    return candidates


def _similar_names(html_text, base_url):
    if not BeautifulSoup:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    names = []
    for anchor in soup.select("a[href]"):
        url = absolute_url(base_url, anchor.get("href"))
        text = clean_text(anchor.get_text(" "))
        if "/p/" in url and 15 <= len(text) <= 180 and not _similar_name_noise(text):
            names.append(text)
    return list(dict.fromkeys(names))[:20]


def _similar_name_noise(text):
    normalized = remove_accents(str(text or "").lower())
    noise_markers = (
        "boas vindas",
        "entre ou cadastre",
        "cadastre-se",
        "digite seu cep",
        "atendimento",
    )
    return any(marker in normalized for marker in noise_markers)


def _merge_jsonld_detail(row, html_text):
    for block in extract_jsonld(html_text):
        if not isinstance(block, dict) or block.get("@type") != "Product":
            continue
        row["retailer_sku_name"] = row.get("retailer_sku_name") or clean_text(block.get("name"))
        row["sku"] = row.get("sku") or clean_text(block.get("sku"))
        offers = block.get("offers") if isinstance(block.get("offers"), dict) else {}
        if offers.get("price"):
            row["final_sku_price"] = str(offers.get("price"))
        rating = block.get("aggregateRating") if isinstance(block.get("aggregateRating"), dict) else {}
        row["star_rating"] = row.get("star_rating") or clean_text(rating.get("ratingValue"))
        row["count_of_star_ratings"] = row.get("count_of_star_ratings") or clean_text(rating.get("ratingCount"))
        row["count_of_reviews"] = row.get("count_of_reviews") or clean_text(rating.get("reviewCount"))
        break


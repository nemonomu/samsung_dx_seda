import csv
import ast
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DEFAULT_RUNS_BASE = PACKAGE_DIR / "data"
DEFAULT_PRODUCT_LINE = "TV"
DEFAULT_COUNTRY = "SEDA"
DEFAULT_POSTAL_CODE = "01001-001"
DEFAULT_OUTPUT_TABLE = "tv_retail_com_seda"


def env_candidate_paths(path=None):
    if path:
        return [Path(path)]
    override = os.getenv("SEDA_ENV_PATH", "").strip()
    if override:
        return [Path(override)]
    return [PACKAGE_DIR / ".env", PROJECT_ROOT / ".env"]


def load_env(path=None):
    env_path = next((candidate for candidate in env_candidate_paths(path) if candidate.exists()), None)
    if env_path is None:
        return
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value == "{":
            collected = ["{"]
            depth = 1
            while i < len(lines) and depth > 0:
                part = lines[i]
                i += 1
                collected.append(part)
                depth += part.count("{") - part.count("}")
            value = "\n".join(collected)
        else:
            value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env()


@dataclass(frozen=True)
class RetailerConfig:
    key: str
    name: str
    base_url: str
    main_url: str
    bsr_url: str


RETAILERS = {
    "magalu": RetailerConfig(
        key="magalu",
        name="Magalu",
        base_url="https://www.magazineluiza.com.br",
        main_url=os.getenv("SEDA_MAGALU_MAIN_URL", "https://www.magazineluiza.com.br/busca/tv/"),
        bsr_url=os.getenv(
            "SEDA_MAGALU_BSR_URL",
            "https://www.magazineluiza.com.br/busca/tv/?sortType=soldQuantity&sortOrientation=desc",
        ),
    ),
    "casas_bahia": RetailerConfig(
        key="casas_bahia",
        name="Casas Bahia",
        base_url="https://www.casasbahia.com.br",
        main_url=os.getenv("SEDA_CASAS_BAHIA_MAIN_URL", "https://www.casasbahia.com.br/tv/b"),
        bsr_url=os.getenv(
            "SEDA_CASAS_BAHIA_BSR_URL",
            "https://www.casasbahia.com.br/tv/b?ordenacao=maisvendidos",
        ),
    ),
}


OUTPUT_COLUMNS = [
    "retailer",
    "country",
    "product_line",
    "item",
    "category",
    "main_rank",
    "bsr_rank",
    "product_url",
    "retailer_sku_name",
    "original_sku_price",
    "final_sku_price",
    "savings",
    "sku_status",
    "discount_type",
    "delivery_availability",
    "pick_up_availability",
    "sku",
    "screen_size",
    "estimated_annual_electricity_use",
    "model_year",
    "summarized_review_content",
    "retailer_sku_name_similar",
    "star_rating",
    "count_of_star_ratings",
    "count_of_reviews",
    "recommendation_intent",
    "detailed_review_content",
    "source_url",
    "crawl_datetime",
    "fetch_method",
    "parse_status",
    "retailer_product_id",
    "seller_id",
]


def run_date():
    return os.getenv("SEDA_RUN_DATE", datetime.now().strftime("%Y%m%d"))


def run_root(run_date_value=None):
    return Path(os.getenv("SEDA_RUN_ROOT", DEFAULT_RUNS_BASE / (run_date_value or run_date())))


def output_table():
    return os.getenv("SEDA_DB_FINAL_TABLE", os.getenv("SEDA_OUTPUT_TABLE", DEFAULT_OUTPUT_TABLE)).strip()


def selected_retailers():
    raw = os.getenv("SEDA_RETAILERS", "magalu,casas_bahia")
    keys = [item.strip().lower() for item in raw.split(",") if item.strip()]
    unknown = [key for key in keys if key not in RETAILERS]
    if unknown:
        raise SystemExit(f"Unknown SEDA retailer(s): {unknown}. Valid: {sorted(RETAILERS)}")
    return keys


def page_url(config, page, run_id="main"):
    base = config.bsr_url if run_id == "bsr" else config.main_url
    if "{page}" in base:
        return base.format(page=page)
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode({'page': page})}" if page > 1 else base


def write_csv(path, rows, columns=OUTPUT_COLUMNS):
    if os.getenv("SEDA_TRANSLATE_OUTPUT", "1").lower() not in {"0", "false", "no", "n"}:
        from .common.translations import translate_row

        rows = [translate_row(row) for row in rows]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def read_csv(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def normalized_product_url(url):
    if not url:
        return ""
    parsed = urlsplit(str(url).strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(query), ""))


def product_identity(row):
    url = normalized_product_url(row.get("product_url", ""))
    return (row.get("retailer", ""), url or row.get("sku", ""))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}


def csv_count(path):
    path = Path(path)
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        return sum(1 for _ in reader)


def _read_multiline_env_object(name):
    raw = os.getenv(name)
    if raw and raw.strip() not in {"{", ""}:
        return raw
    env_path = next((candidate for candidate in env_candidate_paths() if candidate.exists()), None)
    if env_path is None:
        return raw or ""
    lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    collecting = False
    collected = []
    depth = 0
    for line in lines:
        stripped = line.strip()
        if not collecting and stripped.startswith(name) and "=" in stripped:
            value = line.split("=", 1)[1].strip()
            collecting = True
            collected.append(value)
            depth += value.count("{") - value.count("}")
            if depth <= 0 and value:
                break
            continue
        if collecting:
            collected.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
    return "\n".join(collected).strip()


def db_config():
    raw = _read_multiline_env_object("DB_CONFIG")
    if not raw:
        return {}
    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(raw)
            return value if isinstance(value, dict) else {}
        except Exception:
            continue
    return {}


def db_connect():
    config = db_config()
    if not config:
        raise RuntimeError("DB_CONFIG is not set")
    try:
        import psycopg2
    except ImportError as exc:
        raise RuntimeError("psycopg2 is required for DB steps") from exc
    database = config.get("database") or config.get("dbname")
    if not database:
        raise RuntimeError("DB_CONFIG database/dbname is required for PostgreSQL")
    return psycopg2.connect(
        host=config.get("host"),
        port=int(config.get("port") or 5432),
        dbname=database,
        user=config.get("user"),
        password=config.get("password"),
        connect_timeout=int(os.getenv("SEDA_DB_CONNECT_TIMEOUT", "10")),
    )


def safe_status_message(message):
    stream = sys.stderr if "failed" in message.lower() else sys.stdout
    print(f"[seda] {message}", file=stream, flush=True)

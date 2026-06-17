import json
import os
from pathlib import Path

from .parsers import parse_listing
from .step00_config import RETAILERS, OUTPUT_COLUMNS, page_url, product_identity, run_root, selected_retailers, write_csv
from .transport import fetch_url


def page_numbers(run_id=None):
    is_bsr = (run_id or "").lower() == "bsr"
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    raw = os.getenv(f"{prefix}_PAGE_LIST", os.getenv("SEDA_PAGE_LIST", "")).strip()
    if raw:
        return [int(item.strip()) for item in raw.split(",") if item.strip()]
    start = int(os.getenv(f"{prefix}_START_PAGE", os.getenv("SEDA_START_PAGE", "1")))
    target = unique_target(run_id)
    default_pages = max_pages(is_bsr) if target else _default_pages(is_bsr)
    pages = int(os.getenv(f"{prefix}_PAGES", os.getenv("SEDA_PAGES", default_pages)))
    return list(range(start, start + pages))


def _default_pages(is_bsr):
    retailer = os.getenv("SEDA_ACTIVE_RETAILER", "").strip().lower()
    if retailer == "casas_bahia":
        return "7" if is_bsr else "20"
    return "3" if is_bsr else "9"


def unique_target(run_id=None):
    is_bsr = (run_id or "").lower() == "bsr"
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    raw = os.getenv(f"{prefix}_UNIQUE_TARGET", os.getenv("SEDA_UNIQUE_TARGET", "")).strip()
    if raw:
        return int(raw)
    return 0


def max_pages(is_bsr):
    prefix = "SEDA_BSR" if is_bsr else "SEDA_MAIN"
    default = "40" if is_bsr else "120"
    return int(os.getenv(f"{prefix}_MAX_PAGES", os.getenv("SEDA_MAX_PAGES", default)))


def main():
    run_id = os.getenv("SEDA_RUN_ID", "main").strip().lower()
    root = run_root() / run_id
    rows = []
    failures = []
    rank_offsets = {}
    attempted_pages = []
    target = unique_target(run_id)
    no_growth_limit = int(os.getenv("SEDA_UNIQUE_NO_GROWTH_LIMIT", "5"))
    for retailer_key in selected_retailers():
        config = RETAILERS[retailer_key]
        rank_offsets.setdefault(retailer_key, 0)
        unique_seen = set()
        no_growth_pages = 0
        for page in page_numbers(run_id):
            url = page_url(config, page, run_id=run_id)
            attempted_pages.append(page)
            before_unique = len(unique_seen)
            raw_path = root / "raw" / retailer_key / f"page_{page:03d}.html"
            reuse_raw = os.getenv("SEDA_REUSE_RAW", "0").lower() in {"1", "true", "yes", "y"}
            if reuse_raw and raw_path.exists():
                text = raw_path.read_text(encoding="utf-8", errors="ignore")
                method = "raw_reparse"
                attempts = []
                error = ""
            else:
                result = fetch_url(url)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(result.text or result.error, encoding="utf-8", errors="ignore")
                text = result.text
                method = result.method
                attempts = result.attempts
                error = result.error
            if not text or error:
                failures.append(
                    {
                        "retailer": config.name,
                        "page": page,
                        "url": url,
                        "error": error,
                        "attempts": attempts,
                    }
                )
                continue
            parsed = parse_listing(text, config.name, config.base_url, url, run_id=run_id)
            rank_field = "bsr_rank" if run_id == "bsr" else "main_rank"
            for local_index, item in enumerate(parsed, start=1):
                item["fetch_method"] = method
                item[rank_field] = rank_offsets[retailer_key] + local_index
            rank_offsets[retailer_key] += len(parsed)
            rows.extend(parsed)
            for item in parsed:
                key = product_identity(item)
                if key[1]:
                    unique_seen.add(key)
            unique_count = len(unique_seen)
            if unique_count == before_unique:
                no_growth_pages += 1
            else:
                no_growth_pages = 0
            print(
                f"[seda] {run_id} {config.name} page={page} rows={len(parsed)} "
                f"unique={unique_count} method={method}",
                flush=True,
            )
            if target and unique_count >= target:
                print(f"[seda] {run_id} {config.name} reached unique target {target}", flush=True)
                break
            if target and no_growth_pages >= no_growth_limit:
                print(
                    f"[seda] {run_id} {config.name} stopped after {no_growth_pages} pages without unique growth",
                    flush=True,
                )
                break

    parsed_dir = root / "parsed"
    write_csv(parsed_dir / "main_occurrences.csv", rows, columns=OUTPUT_COLUMNS)
    manifest = {
        "run_id": run_id,
        "rows": len(rows),
        "failures": failures,
        "retailers": selected_retailers(),
        "pages": attempted_pages,
        "unique_target": target,
        "fetch_mode": os.getenv("SEDA_FETCH_MODE", "uc_first"),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[seda] wrote {parsed_dir / 'main_occurrences.csv'} rows={len(rows)}")


if __name__ == "__main__":
    main()


import argparse
import asyncio
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

from seda.step00_config import run_root

from .listing_badge_cdp import run as run_badge_sampler


def default_input():
    return str(run_root() / "output" / "final_output_delivery_backfilled.csv")


def default_output():
    return str(run_root() / "output" / "final_output_badged.csv")


def default_raw_dir():
    return str(run_root() / "output" / "listing_badge_cdp_pages")


async def run(args):
    rows, fieldnames = _read_csv(Path(args.input))
    listing_urls = _listing_urls(rows, args)
    if args.limit_urls:
        listing_urls = listing_urls[: args.limit_urls]

    stats = Counter(rows=len(rows), listing_urls=len(listing_urls))
    badge_by_url = {}
    badge_by_item = {}
    savings_by_url = {}
    savings_by_item = {}
    sampled_urls = set()
    errors = []

    for index, listing_url in enumerate(listing_urls, start=1):
        raw_stem = _raw_stem(index, listing_url)
        sample_args = _sample_args(args, listing_url, raw_stem)
        try:
            payload = await run_badge_sampler(sample_args)
        except Exception as exc:  # noqa: BLE001 - keep the full run moving and report in manifest.
            stats.update(sample_failed=1)
            errors.append({"listing_url": listing_url, "error": str(exc)})
            print(f"[casas_badge_backfill] {index}/{len(listing_urls)} failed url={listing_url} error={exc}", flush=True)
            continue
        sampled_urls.add(_normalize_url(listing_url))
        merged = payload.get("merged") or []
        stats.update(sampled=1, sampled_cards=len(merged))
        page_badges = 0
        page_savings = 0
        for card in merged:
            badge = _badge_value(card.get("badges") or [])
            savings = _badge_value(card.get("savings") or [])
            product_url = _normalize_url(card.get("product_url", ""))
            item = str(card.get("item") or _item_from_url(product_url)).strip()
            if badge:
                page_badges += 1
            if savings:
                page_savings += 1
            if product_url and badge:
                badge_by_url[product_url] = badge
            if item and badge:
                badge_by_item[item] = badge
            if product_url and savings:
                savings_by_url[product_url] = savings
            if item and savings:
                savings_by_item[item] = savings
        stats.update(sampled_badge_cards=page_badges)
        stats.update(sampled_savings_cards=page_savings)
        print(
            "[casas_badge_backfill] "
            f"{index}/{len(listing_urls)} cards={len(merged)} badge_cards={page_badges} "
            f"savings_cards={page_savings} url={listing_url}",
            flush=True,
        )

    for row in rows:
        product_url = _normalize_url(row.get("product_url", ""))
        item = str(row.get("item") or _item_from_url(product_url)).strip()
        current = str(row.get("discount_type") or "").strip()
        invalid_current = _is_price_discount(current)
        badge = badge_by_url.get(product_url) or badge_by_item.get(item, "")
        current_savings = str(row.get("savings") or "").strip()
        savings = savings_by_url.get(product_url) or savings_by_item.get(item, "")
        source_url = _normalize_url(row.get("source_url", ""))

        if badge and (args.force or not current or invalid_current):
            row["discount_type"] = badge
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_listing_badge_cdp")
            row["parse_status"] = _append_token(row.get("parse_status", ""), "badge_cdp_ok")
            stats.update(updated=1)
            if invalid_current:
                stats.update(replaced_invalid=1)

        if invalid_current and not badge:
            row["discount_type"] = ""
            row["parse_status"] = _append_token(row.get("parse_status", ""), "badge_cdp_cleared_price_discount")
            stats.update(cleared_invalid=1)

        if source_url in sampled_urls and not badge:
            stats.update(sampled_no_badge=1)

        if savings and (args.force or current_savings != savings):
            row["savings"] = savings
            row["fetch_method"] = _append_token(row.get("fetch_method", ""), "casas_bahia_listing_savings_cdp")
            row["parse_status"] = _append_token(row.get("parse_status", ""), "savings_cdp_ok")
            stats.update(updated_savings=1)
            continue

        if source_url in sampled_urls and current_savings and not savings:
            row["savings"] = ""
            row["parse_status"] = _append_token(row.get("parse_status", ""), "savings_cdp_cleared_not_rendered")
            stats.update(cleared_savings_not_rendered=1)

    _write_csv(Path(args.output), rows, fieldnames)
    manifest = {
        "input": args.input,
        "output": args.output,
        "raw_dir": args.raw_dir,
        "stats": dict(stats),
        "listing_urls": listing_urls,
        "errors": errors[:100],
    }
    manifest_path = Path(args.output).with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _sample_args(args, listing_url, raw_stem):
    raw_dir = Path(args.raw_dir)
    return SimpleNamespace(
        cdp_url=args.cdp_url,
        cdp_start_timeout=args.cdp_start_timeout,
        no_auto_start_cdp=args.no_auto_start_cdp,
        url=listing_url,
        output_json=str(raw_dir / f"{raw_stem}.json"),
        output_csv=str(raw_dir / f"{raw_stem}.csv"),
        samples=args.samples,
        interval_ms=args.interval_ms,
        wait_ms=args.wait_ms,
        timeout_ms=args.timeout_ms,
        scrolls=args.scrolls,
        scroll_pixels=args.scroll_pixels,
        scroll_wait_ms=args.scroll_wait_ms,
        width=args.width,
        height=args.height,
        close_browser=False,
    )


def _listing_urls(rows, args):
    if args.listing_url:
        return _unique(args.listing_url)
    urls = []
    for row in rows:
        url = str(row.get("source_url") or "").strip()
        if url:
            urls.append(url)
    return _unique(urls)


def _unique(values):
    seen = set()
    result = []
    for value in values:
        normalized = _normalize_url(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


def _badge_value(values):
    badges = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in badges:
            badges.append(text)
    return "; ".join(badges)


def _raw_stem(index, listing_url):
    digest = hashlib.sha1(listing_url.encode("utf-8")).hexdigest()[:10]
    return f"listing_badge_{index:03d}_{digest}"


def _is_price_discount(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?%\s*(?:OFF|discount)", text, re.I))


def _append_token(value, token):
    tokens = [part for part in str(value or "").split("+") if part]
    if token not in tokens:
        tokens.append(token)
    return "+".join(tokens)


def _item_from_url(url):
    match = re.search(r"/p/([^/?#]+)", str(url or ""))
    return match.group(1) if match else ""


def _normalize_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))


def _read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def _write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Backfill Casas Bahia rendered listing badge values through Chrome CDP.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--cdp-start-timeout", type=int, default=20)
    parser.add_argument("--no-auto-start-cdp", action="store_true")
    parser.add_argument("--input", default=default_input())
    parser.add_argument("--output", default=default_output())
    parser.add_argument("--raw-dir", default=default_raw_dir())
    parser.add_argument("--listing-url", action="append", default=[])
    parser.add_argument("--limit-urls", type=int, default=0)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-ms", type=int, default=2000)
    parser.add_argument("--wait-ms", type=int, default=7000)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--scrolls", type=int, default=2)
    parser.add_argument("--scroll-pixels", type=int, default=900)
    parser.add_argument("--scroll-wait-ms", type=int, default=1200)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(json.dumps({"stats": result.get("stats", {}), "output": result.get("output", args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

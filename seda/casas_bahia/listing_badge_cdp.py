import argparse
import asyncio
import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DEFAULT_URL = "https://www.casasbahia.com.br/tv/b"
DEFAULT_OUTPUT_JSON = "seda/casas_bahia/test/output/listing_badge_cdp_probe.json"
DEFAULT_OUTPUT_CSV = "seda/casas_bahia/test/output/listing_badge_cdp_probe.csv"


async def run(args):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(args.cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
        page = await context.new_page()
        try:
            await page.set_viewport_size({"width": args.width, "height": args.height})
            await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            await page.wait_for_timeout(args.wait_ms)
            await _scroll_warmup(page, args)
            snapshots = []
            for index in range(args.samples):
                if index:
                    await page.wait_for_timeout(args.interval_ms)
                snapshot = await _sample_page(page)
                snapshot["sample_index"] = index + 1
                snapshot["sample_datetime"] = datetime.now().isoformat(timespec="seconds")
                snapshots.append(snapshot)
                print(
                    "[casas_badge_cdp] "
                    f"sample={index + 1}/{args.samples} cards={len(snapshot.get('cards') or [])} "
                    f"badges={sum(len(card.get('badges') or []) for card in snapshot.get('cards') or [])}",
                    flush=True,
                )
        finally:
            await page.close()
            if args.close_browser:
                await browser.close()

    merged = _merge_snapshots(args.url, snapshots)
    payload = {"url": args.url, "snapshots": snapshots, "merged": merged}
    _write_json(Path(args.output_json), payload)
    _write_csv(Path(args.output_csv), merged)
    return payload


async def _scroll_warmup(page, args):
    for _ in range(max(0, args.scrolls)):
        await page.mouse.wheel(0, args.scroll_pixels)
        await page.wait_for_timeout(args.scroll_wait_ms)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(args.scroll_wait_ms)


async def _sample_page(page):
    script = r"""
    () => {
      const badgePatterns = [
        /\b\d+(?:[,.]\d+)?%\s+(?:de\s+)?desconto\b/i,
        /\buse\s+o\s+cupom\s+[A-Za-z0-9_-]+\b/i,
        /\bdesconto\s+(?:pagando\s+com\s+)?carn[eê]\s+digital\b/i,
        /\bdesconto\s+no\s+carn[eê]\b/i,
      ];
      const blockedPatterns = [
        /\bbaixou\s+\d/i,
        /\bno\s+pix\b/i,
        /\bsem\s+juros\b/i,
        /\bat[eé]\s+\d+x\b/i,
        /\br\$\s*[\d.,]+/i,
      ];
      const productCards = [...document.querySelectorAll('[data-testid="product-card-item"]')];
      if (!productCards.length) {
        productCards.push(...document.querySelectorAll('[data-testid="product-card-desktop"]'));
      }
      const productAnchors = [...document.querySelectorAll('a[href*="/p/"]')];
      const seen = new Map();
      function normalizeText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
      }
      function normalizeUrl(value) {
        try {
          const url = new URL(value, location.origin);
          url.hash = '';
          url.search = '';
          return url.href;
        } catch (error) {
          return '';
        }
      }
      function productId(value) {
        const match = String(value || '').match(/\/p\/([^/?#]+)/);
        return match ? match[1] : '';
      }
      function climbCard(anchor) {
        let node = anchor;
        let best = anchor;
        for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
          const text = normalizeText(node.innerText || node.textContent || '');
          if (text.includes('R$') || text.match(/\b(?:Smart\s+TV|TV)\b/i)) {
            best = node;
          }
          const links = node.querySelectorAll ? node.querySelectorAll('a[href*="/p/"]').length : 0;
          if (links > 1 && depth > 0) {
            break;
          }
        }
        return best;
      }
      function nodeTexts(root) {
        const values = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
        let current = root;
        while (current) {
          const direct = [...current.childNodes]
            .filter(node => node.nodeType === Node.TEXT_NODE)
            .map(node => normalizeText(node.textContent))
            .filter(Boolean)
            .join(' ');
          if (direct) values.push(direct);
          for (const attr of ['aria-label', 'alt', 'title']) {
            const value = normalizeText(current.getAttribute && current.getAttribute(attr));
            if (value) values.push(value);
          }
          current = walker.nextNode();
        }
        return values;
      }
      function extractBadges(card) {
        const candidates = [];
        for (const text of nodeTexts(card)) {
          for (const pattern of badgePatterns) {
            const matches = text.match(new RegExp(pattern.source, pattern.flags.includes('g') ? pattern.flags : pattern.flags + 'g')) || [];
            for (const match of matches) candidates.push(normalizeText(match));
          }
        }
        return [...new Set(candidates)]
          .filter(value => value && !blockedPatterns.some(pattern => pattern.test(value)));
      }
      function extractName(card, anchor) {
        const anchorText = normalizeText(anchor.innerText || anchor.textContent || '');
        if (anchorText && !anchorText.includes('R$')) return anchorText.slice(0, 240);
        const cardText = normalizeText(card.innerText || card.textContent || '');
        const beforePrice = cardText.split('R$')[0] || cardText;
        return beforePrice.slice(0, 240);
      }
      const sources = productCards.length
        ? productCards.map(card => ({card, anchor: card.querySelector('a[href*="/p/"]')})).filter(item => item.anchor)
        : productAnchors.map(anchor => ({anchor, card: climbCard(anchor)}));
      for (const source of sources) {
        const anchor = source.anchor;
        const url = normalizeUrl(anchor.href);
        if (!url || seen.has(url)) continue;
        const card = source.card || climbCard(anchor);
        seen.set(url, {
          product_url: url,
          item: productId(url),
          name: extractName(card, anchor),
          badges: extractBadges(card),
          card_text_preview: normalizeText(card.innerText || card.textContent || '').slice(0, 700),
        });
      }
      return {
        href: location.href,
        title: document.title,
        cards: [...seen.values()],
      };
    }
    """
    return await page.evaluate(script)


def _merge_snapshots(source_url, snapshots):
    cards = {}
    badge_samples = defaultdict(list)
    for snapshot in snapshots:
        sample_index = snapshot.get("sample_index")
        for card in snapshot.get("cards") or []:
            url = _normalize_url(card.get("product_url"))
            if not url:
                continue
            cards.setdefault(
                url,
                {
                    "source_url": source_url,
                    "product_url": url,
                    "item": card.get("item", ""),
                    "name": card.get("name", ""),
                    "badges": [],
                    "sample_hits": [],
                    "card_text_preview": card.get("card_text_preview", ""),
                },
            )
            if card.get("name") and not cards[url].get("name"):
                cards[url]["name"] = card.get("name", "")
            if card.get("card_text_preview"):
                cards[url]["card_text_preview"] = card.get("card_text_preview", "")
            for badge in card.get("badges") or []:
                clean = _clean_badge(badge)
                if clean:
                    badge_samples[url].append((clean, sample_index))
    for url, values in badge_samples.items():
        badges = []
        sample_hits = []
        for badge, sample_index in values:
            if badge not in badges:
                badges.append(badge)
            sample_hits.append(f"{sample_index}:{badge}")
        cards[url]["badges"] = badges
        cards[url]["sample_hits"] = sample_hits
    return list(cards.values())


def _clean_badge(value):
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    text = re.sub(r"\b(\d+(?:[,.]\d+)?)%\s+(?:de\s+)?desconto\b", lambda m: f"{m.group(1).replace(',', '.')}% De DESCONTO", text, flags=re.I)
    text = re.sub(r"\buse\s+o\s+cupom\b", "Use o cupom", text, flags=re.I)
    text = re.sub(r"\bdesconto\b", "Desconto", text, flags=re.I)
    return text


def _normalize_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["source_url", "product_url", "item", "name", "discount_type", "sample_hits", "card_text_preview"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source_url": row.get("source_url", ""),
                    "product_url": row.get("product_url", ""),
                    "item": row.get("item", ""),
                    "name": row.get("name", ""),
                    "discount_type": "; ".join(row.get("badges") or []),
                    "sample_hits": "; ".join(row.get("sample_hits") or []),
                    "card_text_preview": row.get("card_text_preview", ""),
                }
            )


def main():
    parser = argparse.ArgumentParser(description="Sample rendered Casas Bahia listing product badges through Chrome CDP.")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output-json", default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-ms", type=int, default=2000)
    parser.add_argument("--wait-ms", type=int, default=7000)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--scrolls", type=int, default=2)
    parser.add_argument("--scroll-pixels", type=int, default=900)
    parser.add_argument("--scroll-wait-ms", type=int, default=1200)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--close-browser", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    badge_rows = [row for row in result.get("merged", []) if row.get("badges")]
    print(
        json.dumps(
            {
                "cards": len(result.get("merged", [])),
                "badge_rows": len(badge_rows),
                "output_json": args.output_json,
                "output_csv": args.output_csv,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

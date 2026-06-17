import html
import json
import re
from collections import Counter
from pathlib import Path

from seda.common.har_tools import decoded_response_text


REFERENCES = Path("references")
PATTERNS = [
    "A maioria dos consumidores",
    "A qualidade de imagem",
    "summary-detail-description",
    "ReviewSummary",
    "reviewSummary",
    "productRating",
    "summary",
    "summarized",
    "Resumo de avaliações",
]


def main():
    inspect_next_data(REFERENCES / "magalu_ai_summary_response.html")
    inspect_har_hits(
        [
            "magalu_ai_reviews_all.har",
            "magalu_detail_all.har",
            "magalu_reviews_all.har",
            "magalu_availability_all.har",
            "magalu_main_sku_status_all.har",
        ]
    )
    inspect_operations(
        [
            "magalu_ai_reviews_all.har",
            "magalu_detail_all.har",
            "magalu_reviews_all.har",
            "magalu_availability_all.har",
            "magalu_main_sku_status_all.har",
        ]
    )
    inspect_js_chunks(
        [
            "magalu_ai_reviews_all.har",
            "magalu_ai_summary_response.html",
        ]
    )


def inspect_next_data(path):
    print("\n== NEXT_DATA paths ==")
    text = path.read_text(encoding="utf-8", errors="ignore")
    data = _next_data(text)
    hits = []

    def walk(value, path_value=""):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path_value}.{key}" if path_value else key
                child_text = child if isinstance(child, str) else ""
                if _interesting_key(key) or _interesting_text(child_text):
                    hits.append((child_path, type(child).__name__, _sample(child)))
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path_value}[{index}]")

    walk(data)
    print(f"hits={len(hits)}")
    for item in hits[:200]:
        print(_ascii(repr(item)))


def inspect_har_hits(files):
    print("\n== HAR response hits ==")
    for name in files:
        path = REFERENCES / name
        if not path.exists():
            continue
        har = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
        print(f"\nFILE {name}")
        for index, entry in enumerate(har.get("log", {}).get("entries", [])):
            url = (entry.get("request") or {}).get("url", "")
            text = decoded_response_text(entry)
            found = [pattern for pattern in PATTERNS if pattern.lower() in text.lower()]
            if found:
                print(_ascii(f"{index} patterns={found} len={len(text)} url={url[:180]}"))


def inspect_operations(files):
    print("\n== GraphQL operation names ==")
    for name in files:
        path = REFERENCES / name
        if not path.exists():
            continue
        har = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
        ops = []
        for entry in har.get("log", {}).get("entries", []):
            request = entry.get("request") or {}
            url = request.get("url", "")
            post = (request.get("postData") or {}).get("text", "") or ""
            if "operationName=" in url:
                ops.append(url.split("operationName=", 1)[1].split("&", 1)[0])
            if post.strip().startswith("{"):
                try:
                    ops.append(json.loads(post).get("operationName"))
                except ValueError:
                    pass
        print(f"{name}: {Counter(op for op in ops if op).most_common()}")


def inspect_js_chunks(files):
    print("\n== JS/chunk query hints ==")
    chunks = []
    for name in files:
        path = REFERENCES / name
        if name.endswith(".har") and path.exists():
            har = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
            for entry in har.get("log", {}).get("entries", []):
                url = (entry.get("request") or {}).get("url", "")
                if ".js" not in url:
                    continue
                text = decoded_response_text(entry)
                chunks.append((name, url, text))
        elif name.endswith(".html") and path.exists():
            # The response HTML references chunks but does not contain their bodies.
            pass

    terms = ["ReviewSummary", "summary", "productRating", "ProductRating", "reviews", "reviewSummary"]
    for source, url, text in chunks:
        low = text.lower()
        found = [term for term in terms if term.lower() in low]
        if not found:
            continue
        print(_ascii(f"\nCHUNK source={source} len={len(text)} found={found} url={url[:180]}"))
        for term in found[:4]:
            for pos in _positions(low, term.lower(), limit=3):
                start = max(0, pos - 260)
                end = min(len(text), pos + 520)
                print(_ascii(f"--- term={term} pos={pos} --- {text[start:end]}"))


def _next_data(text):
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S | re.I)
    if not match:
        return {}
    return json.loads(html.unescape(match.group(1)))


def _interesting_key(key):
    key = str(key or "").lower()
    return any(term in key for term in ("summary", "review", "rating"))


def _interesting_text(text):
    lower = str(text or "").lower()
    return any(pattern.lower() in lower for pattern in PATTERNS)


def _sample(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)[:500]
    return str(value)[:500]


def _positions(text, term, limit=3):
    positions = []
    start = 0
    while len(positions) < limit:
        pos = text.find(term, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + len(term)
    return positions


def _ascii(value):
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    main()

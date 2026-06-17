import json
from pathlib import Path

from seda.common.har_tools import decoded_response_text


TERMS = [
    "reviewSummaryQuery",
    "reviewSummary",
    "ReviewSummary",
    "summary-detail-description",
    "Resumo de avaliações de clientes feito pela Lu",
    "summary:",
    "summary}",
    "summary,",
]


def main():
    for har_name in [
        "magalu_ai_reviews_all.har",
        "magalu_detail_all.har",
        "magalu_main_sku_status_all.har",
    ]:
        search_har(har_name)


def search_har(har_name):
    print(f"\n== {har_name} ==")
    path = Path("references") / har_name
    har = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
    for entry in har.get("log", {}).get("entries", []):
        url = (entry.get("request") or {}).get("url", "")
        if ".js" not in url:
            continue
        text = decoded_response_text(entry)
        low = text.lower()
        found = [term for term in TERMS if term.lower() in low]
        if not found:
            continue
        print(_ascii(f"URL {url} len={len(text)} found={found}"))
        for term in found:
            positions = _positions(low, term.lower(), limit=8)
            print(_ascii(f"term={term} positions={positions}"))
            for pos in positions[:3]:
                snippet = text[max(0, pos - 900) : min(len(text), pos + 1400)]
                print(_ascii(snippet))
                print("---")


def _positions(text, term, limit=8):
    out = []
    pos = 0
    while len(out) < limit:
        pos = text.find(term, pos)
        if pos < 0:
            break
        out.append(pos)
        pos += len(term)
    return out


def _ascii(value):
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


if __name__ == "__main__":
    main()

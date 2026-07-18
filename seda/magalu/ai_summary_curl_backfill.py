import argparse
import re
import subprocess
from pathlib import Path

import pandas as pd
import requests

from ..parsers import parse_detail, sku_from_url
from ..step08_detail_enrichment import _detail_identity_mode


def main():
    parser = argparse.ArgumentParser(description="Backfill Magalu AI review summaries from Copy-as-cURL text.")
    parser.add_argument("curl_txt")
    parser.add_argument("--csv", default="seda/data/magalu/20260603/output/final_output_enriched.csv")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--transport", choices=["requests", "curl"], default="requests")
    args = parser.parse_args()

    curls = _curl_blocks(Path(args.curl_txt).read_text(encoding="utf-8", errors="ignore"))
    pdp_requests = [_parse_pdp_curl(block) for block in curls]
    pdp_requests = [request for request in pdp_requests if request]
    print(f"[seda] pdp_curl_requests={len(pdp_requests)}")

    summary_details = {}
    for request in pdp_requests:
        item = sku_from_url(request["url"])
        if not item or item in summary_details:
            continue
        try:
            response = _fetch(request, args.timeout, args.transport)
        except Exception as exc:
            print(f"[seda] ai_summary item={item} status=0 error={type(exc).__name__}")
            continue
        detail = parse_detail(response["text"] or "", "Magalu", "https://www.magazineluiza.com.br", request["url"])
        summary = detail.get("summarized_review_content", "")
        if summary:
            summary_details[item] = detail
        print(
            f"[seda] ai_summary item={item} status={response['status_code']} "
            f"html_len={len(response['text'] or '')} summary_len={len(summary)}"
        )

    if args.dry_run:
        print(f"[seda] dry_run summary_candidates={len(summary_details)}")
        return

    path = Path(args.csv)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df["_item"] = df["product_url"].str.extract(r"/p/([^/]+)/", expand=False).fillna("")
    before = int(df["summarized_review_content"].ne("").sum())
    candidates = df.apply(
        lambda row: _trusted_summary(row, summary_details.get(row.get("_item"), {})),
        axis=1,
    )
    mask = candidates.ne("") & df["summarized_review_content"].eq("")
    df.loc[mask, "summarized_review_content"] = candidates[mask]
    updated = int(mask.sum())
    df = df.drop(columns=["_item"])
    df.to_csv(path, index=False, encoding="utf-8-sig")
    after = int(df["summarized_review_content"].ne("").sum())
    print(f"[seda] wrote {path} before={before} updated={updated} after={after}")


def _trusted_summary(row, detail):
    if not detail or _detail_identity_mode(row, detail) not in {'verified', 'same_name'}:
        return ''
    return str(detail.get('summarized_review_content') or '').strip()


def _curl_blocks(text):
    blocks = []
    current = []
    for line in text.splitlines():
        if line.startswith("curl ") and current:
            blocks.append("\n".join(current))
            current = [line]
        elif line.startswith("curl "):
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _parse_pdp_curl(block):
    match = re.search(r'curl \^"([^"]+)', block)
    if not match:
        return None
    url = _decode_windows_curl(match.group(1))
    if "magazineluiza.com.br" not in url or "/p/" not in url:
        return None
    if any(skip in url for skip in ("/_next/", "/static/", "/a-static/", "/assets/", "/tom/", "/zGOjRql2O3/", "/stw/")):
        return None
    headers = {}
    for header_match in re.finditer(r'-H \^"([^"]+)\^"', block):
        header = _decode_windows_curl(header_match.group(1))
        if ": " not in header:
            continue
        key, value = header.split(": ", 1)
        if key.lower() != "cookie":
            headers[key] = value
    cookie_match = re.search(r'-b \^"([^"]*)\^"', block, re.S)
    if cookie_match:
        headers["Cookie"] = _decode_windows_curl(cookie_match.group(1))
    return {"url": url, "headers": headers}


def _fetch(request, timeout, transport):
    if transport == "curl":
        command = ["curl.exe", "-L", "--compressed", "--max-time", str(timeout)]
        for key, value in request["headers"].items():
            command.extend(["-H", f"{key}: {value}"])
        command.append(request["url"])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=timeout + 10)
        return {"status_code": 0 if completed.returncode else 200, "text": completed.stdout}
    response = requests.get(request["url"], headers=request["headers"], timeout=timeout)
    return {"status_code": response.status_code, "text": response.text}


def _decode_windows_curl(value):
    return (
        value.replace("^&", "&")
        .replace("^%", "%")
        .replace("^#", "#")
        .replace('^"', '"')
        .replace("^^", "^")
    )


if __name__ == "__main__":
    main()

import argparse
import json
from pathlib import Path

from seda.common.har_tools import decoded_response_text
from seda.parsers import parse_listing
from seda.step00_config import OUTPUT_COLUMNS, RETAILERS, write_csv


def rows_from_har(path):
    har = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    config = RETAILERS["magalu"]
    best_rows = []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        response = entry.get("response", {})
        content = response.get("content", {})
        if request.get("method") != "GET":
            continue
        if "www.magazineluiza.com.br/busca/tv" not in request.get("url", ""):
            continue
        if "html" not in str(content.get("mimeType", "")).lower():
            continue
        html = decoded_response_text(entry)
        rows = parse_listing(html, config.name, config.base_url, request.get("url", ""), run_id="main")
        if len(rows) > len(best_rows):
            best_rows = rows
    return best_rows


def main():
    parser = argparse.ArgumentParser(description="Extract Magalu listing rows from HAR HTML __NEXT_DATA__.")
    parser.add_argument("har_path")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    rows = rows_from_har(args.har_path)
    output = args.output or str(Path(args.har_path).with_name(f"{Path(args.har_path).stem}_magalu_listing.csv"))
    write_csv(output, rows, columns=OUTPUT_COLUMNS)
    print(f"[seda] wrote {output} rows={len(rows)}")


if __name__ == "__main__":
    main()

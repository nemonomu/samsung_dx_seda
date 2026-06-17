import argparse
import json
from pathlib import Path

from .common.har_tools import load_har, summarize_har
from .network_capture import summarize_graphql_entries


def main():
    parser = argparse.ArgumentParser(description="Summarize SEDA HAR files without printing headers or cookies.")
    parser.add_argument("paths", nargs="+", help="HAR file paths")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    result = {str(path): summarize_har(path) for path in args.paths}
    graphql = {str(path): summarize_graphql_entries(load_har(path)) for path in args.paths}
    result = {"api_summary": result, "graphql_summary": graphql}
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

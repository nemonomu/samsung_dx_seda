import os
import subprocess

from .detail_publish import detail_consumer_guard
from .step00_config import run_root, write_json


def main():
    root = run_root()
    with detail_consumer_guard(root):
        return _main(root)


def _main(root):
    destination = os.getenv("SEDA_S3_DEST", "").strip()
    output = root / "s3" / "manifest_s3_sync.json"
    if not destination:
        write_json(output, {"success": True, "synced": False, "skip_reason": "SEDA_S3_DEST is not set."})
        print(f"[seda] wrote {output}")
        return
    command = [
        "aws",
        "s3",
        "sync",
        str(root),
        destination,
        "--exclude",
        "detail/trace/detail_publish_transaction.json",
        "--exclude",
        "detail/trace/detail_publish.lock",
        "--exclude",
        "detail/trace/detail_run.lock",
        "--exclude",
        "detail/trace/.detail_publish_transaction.json.*.tmp",
        "--exclude",
        "detail/trace/.*.detail_publish.*",
        "--exclude",
        "detail/trace/subcall_trace_*.csv",
        "--exclude",
        "detail/trace/magalu_review_page_trace_*.csv",
        "--exclude",
        "output/.*.detail_publish.*",
        "--exclude",
        "output/_detail_part_*.csv",
    ]
    if os.getenv("SEDA_S3_DRY_RUN", "0").lower() in {"1", "true", "yes", "y"}:
        command.append("--dryrun")
    code = subprocess.call(command)
    write_json(output, {"success": code == 0, "synced": code == 0, "destination": destination})
    if code:
        raise SystemExit(code)
    print(f"[seda] wrote {output}")


if __name__ == "__main__":
    main()

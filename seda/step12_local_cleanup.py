import os
from datetime import datetime, timedelta

from .step00_config import DEFAULT_RUNS_BASE, run_root, write_json


def main():
    current = run_root().resolve()
    output = current / "cleanup" / "manifest_local_cleanup.json"
    enabled = os.getenv("SEDA_LOCAL_CLEANUP", "0").lower() in {"1", "true", "yes", "y"}
    retention_days = int(os.getenv("SEDA_LOCAL_RETENTION_DAYS", "30"))
    if not enabled:
        write_json(output, {"success": True, "deleted": [], "skip_reason": "SEDA_LOCAL_CLEANUP is disabled."})
        print(f"[seda] wrote {output}")
        return

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = []
    base = DEFAULT_RUNS_BASE.resolve()
    for child in base.iterdir() if base.exists() else []:
        if not child.is_dir() or child.resolve() == current:
            continue
        if datetime.fromtimestamp(child.stat().st_mtime) >= cutoff:
            continue
        for nested in sorted(child.rglob("*"), reverse=True):
            if nested.is_file():
                nested.unlink()
            elif nested.is_dir():
                nested.rmdir()
        child.rmdir()
        deleted.append(str(child))
    write_json(output, {"success": True, "deleted": deleted, "retention_days": retention_days})
    print(f"[seda] wrote {output}")


if __name__ == "__main__":
    main()

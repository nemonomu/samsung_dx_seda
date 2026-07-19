import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .step00_config import DEFAULT_RUNS_BASE, run_root, write_json


_RUN_DIRECTORY_RE = re.compile(r"^\d{8}$")
_RETAILER_DIRECTORIES = ("magalu", "casas_bahia")
_PRODUCT_LINE_DIRECTORIES = ("tv", "ref", "ldy")


def main():
    current = run_root().resolve()
    output = current / "cleanup" / "manifest_local_cleanup.json"
    enabled = os.getenv("SEDA_LOCAL_CLEANUP", "0").lower() in {"1", "true", "yes", "y"}
    if not enabled:
        write_json(output, {"success": True, "deleted": [], "skip_reason": "SEDA_LOCAL_CLEANUP is disabled."})
        print(f"[seda] wrote {output}")
        return

    deleted = []
    skipped_reparse = []
    retention_days = None
    skip_reason = ""
    try:
        retention_days = int(os.getenv("SEDA_LOCAL_RETENTION_DAYS", "30"))
        if retention_days < 0:
            raise ValueError("SEDA_LOCAL_RETENTION_DAYS must be zero or greater")
        cutoff = datetime.now() - timedelta(days=retention_days)
        base = Path(os.path.abspath(DEFAULT_RUNS_BASE))
        if _is_reparse_point(base):
            raise RuntimeError(f"cleanup_reparse_point:{base}")
        resolved_base = base.resolve()
        if not _is_within(current, resolved_base):
            skip_reason = "current_run_outside_default_cleanup_base"
        for child in [] if skip_reason else _run_directories(base, skipped_reparse):
            if _is_reparse_point(child):
                _record_skipped_reparse(skipped_reparse, child)
                continue
            resolved = child.resolve()
            if _paths_overlap(resolved, current):
                continue
            try:
                modified = datetime.fromtimestamp(child.stat().st_mtime)
            except FileNotFoundError:
                continue
            if modified >= cutoff:
                continue
            _remove_run_directory(child, base)
            deleted.append(str(resolved))
    except Exception as exc:
        payload = {
            "success": False,
            "deleted": deleted,
            "skipped_reparse": skipped_reparse,
            "error": f"{type(exc).__name__}:{exc}",
        }
        if retention_days is not None:
            payload["retention_days"] = retention_days
        write_json(output, payload)
        raise
    payload = {
        "success": True,
        "deleted": deleted,
        "skipped_reparse": skipped_reparse,
        "retention_days": retention_days,
    }
    if skip_reason:
        payload["skip_reason"] = skip_reason
    write_json(output, payload)
    print(f"[seda] wrote {output}")


def _run_directories(base, skipped_reparse=None):
    base = Path(base)
    if not base.exists():
        return []
    if _is_reparse_point(base):
        _record_skipped_reparse(skipped_reparse, base)
        return []
    parents = [base]
    parents.extend(base / line for line in _PRODUCT_LINE_DIRECTORIES)
    parents.extend(
        base / retailer / line
        for retailer in _RETAILER_DIRECTORIES
        for line in _PRODUCT_LINE_DIRECTORIES
    )
    candidates = []
    for parent in parents:
        reparse_point = _ancestry_reparse_point(parent, base)
        if reparse_point is not None:
            _record_skipped_reparse(skipped_reparse, reparse_point)
            continue
        if not parent.is_dir():
            continue
        for path in parent.iterdir():
            if _is_reparse_point(path):
                _record_skipped_reparse(skipped_reparse, path)
                continue
            if not path.is_dir() or not _is_run_directory_name(path.name):
                continue
            candidates.append(path)
    return sorted(candidates, key=lambda path: len(path.parts), reverse=True)


def _record_skipped_reparse(skipped_reparse, path):
    if skipped_reparse is None:
        return
    value = str(path)
    if value not in skipped_reparse:
        skipped_reparse.append(value)


def _is_run_directory_name(name):
    if not _RUN_DIRECTORY_RE.fullmatch(str(name or "")):
        return False
    try:
        datetime.strptime(str(name), "%Y%m%d")
    except ValueError:
        return False
    return True


def _paths_overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _remove_run_directory(path, base):
    raw_base = Path(base)
    raw_path = Path(path)
    _assert_non_reparse_ancestry(raw_path, raw_base)
    resolved_base = raw_base.resolve()
    resolved_path = raw_path.resolve()
    if resolved_path == resolved_base or resolved_base not in resolved_path.parents:
        raise RuntimeError(f"cleanup_path_outside_base:{resolved_path}")
    _assert_tree_has_no_reparse_points(raw_path)
    _remove_tree_without_reparse_points(raw_path)


def _assert_non_reparse_ancestry(path, base):
    reparse_point = _ancestry_reparse_point(path, base)
    if reparse_point is not None:
        raise RuntimeError(f"cleanup_reparse_point:{reparse_point}")


def _ancestry_reparse_point(path, base):
    absolute_base = Path(os.path.abspath(base))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_base)
    except ValueError as exc:
        raise RuntimeError(f"cleanup_path_outside_base:{path}") from exc
    current = absolute_base
    if _is_reparse_point(current):
        return current
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            return current
    return None


def _assert_tree_has_no_reparse_points(root):
    pending = [Path(root)]
    while pending:
        directory = pending.pop()
        if _is_reparse_point(directory):
            raise RuntimeError(f"cleanup_reparse_point:{directory}")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse_point(path):
                    raise RuntimeError(f"cleanup_reparse_point:{path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def _remove_tree_without_reparse_points(root):
    pending = [(Path(root), False)]
    while pending:
        path, visited = pending.pop()
        if _is_reparse_point(path):
            raise RuntimeError(f"cleanup_reparse_point:{path}")
        if visited:
            path.rmdir()
            continue
        pending.append((path, True))
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse_point(child):
                    raise RuntimeError(f"cleanup_reparse_point:{child}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((child, False))
                else:
                    child.unlink()


def _is_reparse_point(path):
    path = Path(path)
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_within(path, base):
    return path == base or base in path.parents


if __name__ == "__main__":
    main()

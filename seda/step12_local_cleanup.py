import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path

from .step00_config import DEFAULT_RUNS_BASE, run_root, write_json


_RUN_DIRECTORY_RE = re.compile(r"^\d{8}$")
_RETAILER_DIRECTORIES = ("magalu", "casas_bahia")
_PRODUCT_LINE_DIRECTORIES = ("tv", "ref", "ldy")
_DETAIL_TRACE_FILE_RE = re.compile(
    r"^(?:subcall_trace|magalu_review_page_trace)(?:_[A-Za-z0-9_.-]+)?\.csv$"
)
_DETAIL_TRACE_TEMP_RE = re.compile(
    r"^\.(?:subcall_trace|magalu_review_page_trace)"
    r"(?:_[A-Za-z0-9_.-]+)?\.csv\.\d+\.\d+\.tmp$"
)


def main():
    current = run_root().resolve()
    output = current / "cleanup" / "manifest_local_cleanup.json"
    run_cleanup_enabled = _env_enabled("SEDA_LOCAL_CLEANUP", "0")
    trace_cleanup_enabled = _env_enabled("SEDA_DETAIL_TRACE_CLEANUP", "1")
    deleted = []
    skipped_reparse = []
    retention_days = None
    trace_retention_days = None
    detail_trace_cleanup = {
        "enabled": trace_cleanup_enabled,
        "retention_days": None,
        "deleted_files": [],
        "deleted_bytes": 0,
        "errors": [],
    }
    skip_reason = ""
    try:
        if run_cleanup_enabled:
            retention_days = _retention_days("SEDA_LOCAL_RETENTION_DAYS", "30")
        if trace_cleanup_enabled:
            trace_retention_days = _retention_days(
                "SEDA_DETAIL_TRACE_RETENTION_DAYS",
                "3",
            )
            detail_trace_cleanup["retention_days"] = trace_retention_days

        if not run_cleanup_enabled and not trace_cleanup_enabled:
            payload = {
                "success": True,
                "deleted": [],
                "skipped_reparse": [],
                "detail_trace_cleanup": detail_trace_cleanup,
                "skip_reason": (
                    "SEDA_LOCAL_CLEANUP and SEDA_DETAIL_TRACE_CLEANUP are disabled."
                ),
            }
            write_json(output, payload)
            print(f"[seda] wrote {output}")
            return

        now = datetime.now()
        base = Path(os.path.abspath(DEFAULT_RUNS_BASE))
        try:
            if _is_reparse_point(base):
                raise RuntimeError(f"cleanup_reparse_point:{base}")
            resolved_base = base.resolve()
            if not _is_within(current, resolved_base):
                skip_reason = "current_run_outside_default_cleanup_base"
            run_directories = [] if skip_reason else _run_directories(base, skipped_reparse)
        except (OSError, RuntimeError) as exc:
            if run_cleanup_enabled:
                raise
            _record_trace_cleanup_error(detail_trace_cleanup["errors"], base, exc)
            skip_reason = "detail_trace_cleanup_discovery_failed"
            run_directories = []
        if run_cleanup_enabled:
            cutoff = now - timedelta(days=retention_days)
            for child in run_directories:
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
        if trace_cleanup_enabled and not skip_reason:
            try:
                trace_result = _cleanup_detail_trace_files(
                    run_directories,
                    current=current,
                    base=base,
                    retention_days=trace_retention_days,
                    now=now,
                    skipped_reparse=skipped_reparse,
                    result=detail_trace_cleanup,
                )
                detail_trace_cleanup.update(trace_result)
            except (OSError, RuntimeError) as exc:
                _record_trace_cleanup_error(detail_trace_cleanup["errors"], base, exc)
    except Exception as exc:
        payload = {
            "success": False,
            "deleted": deleted,
            "skipped_reparse": skipped_reparse,
            "detail_trace_cleanup": detail_trace_cleanup,
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
        "detail_trace_cleanup": detail_trace_cleanup,
    }
    if retention_days is not None:
        payload["retention_days"] = retention_days
    if not run_cleanup_enabled:
        payload["run_cleanup_skip_reason"] = "SEDA_LOCAL_CLEANUP is disabled."
    if skip_reason:
        payload["skip_reason"] = skip_reason
    write_json(output, payload)
    print(f"[seda] wrote {output}")


def _env_enabled(name, default):
    return os.getenv(name, default).lower() in {"1", "true", "yes", "y"}


def _retention_days(name, default):
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _cleanup_detail_trace_files(
    run_directories,
    current,
    base,
    retention_days,
    now=None,
    skipped_reparse=None,
    result=None,
):
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    cutoff_date = cutoff.date()
    result = result if result is not None else {}
    deleted_files = result.setdefault("deleted_files", [])
    result.setdefault("deleted_bytes", 0)
    errors = result.setdefault("errors", [])
    current = Path(current).resolve()
    base = Path(base)

    for run_directory in run_directories:
        run_directory = Path(run_directory)
        if not run_directory.exists() or _is_reparse_point(run_directory):
            if _is_reparse_point(run_directory):
                _record_skipped_reparse(skipped_reparse, run_directory)
            continue
        resolved_run = run_directory.resolve()
        if _paths_overlap(resolved_run, current):
            continue
        try:
            run_date = datetime.strptime(run_directory.name, "%Y%m%d").date()
        except ValueError:
            continue
        if run_date > cutoff_date:
            continue

        trace_directory = run_directory / "detail" / "trace"
        reparse_point = _ancestry_reparse_point(trace_directory, base)
        if reparse_point is not None:
            _record_skipped_reparse(skipped_reparse, reparse_point)
            continue
        if not trace_directory.is_dir():
            continue
        try:
            children = list(trace_directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            _record_trace_cleanup_error(errors, trace_directory, exc)
            continue
        if any(_is_reparse_point(path) for path in children):
            for path in children:
                if _is_reparse_point(path):
                    _record_skipped_reparse(skipped_reparse, path)
            continue

        trace_files = [
            path
            for path in children
            if path.is_file() and _is_detail_trace_file(path.name)
        ]
        if not trace_files:
            continue
        trace_stats = []
        stat_failed = False
        for path in trace_files:
            try:
                trace_stats.append((path, path.stat()))
            except FileNotFoundError:
                continue
            except OSError as exc:
                _record_trace_cleanup_error(errors, path, exc)
                stat_failed = True
        if stat_failed:
            continue
        if not trace_stats:
            continue
        latest_modified = max(
            datetime.fromtimestamp(file_stat.st_mtime)
            for _path, file_stat in trace_stats
        )
        if latest_modified > cutoff:
            continue

        for path, file_stat in trace_stats:
            try:
                _assert_non_reparse_ancestry(path, base)
                resolved_path = path.resolve()
                path.unlink()
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError) as exc:
                _record_trace_cleanup_error(errors, path, exc)
                continue
            deleted_files.append(str(resolved_path))
            result["deleted_bytes"] += file_stat.st_size

    return {
        "deleted_files": deleted_files,
        "deleted_bytes": result["deleted_bytes"],
        "errors": errors,
    }


def _record_trace_cleanup_error(errors, path, exc):
    errors.append(
        {
            "path": str(path),
            "error": f"{type(exc).__name__}:{exc}",
        }
    )


def _is_detail_trace_file(name):
    value = str(name or "")
    return bool(
        _DETAIL_TRACE_FILE_RE.fullmatch(value)
        or _DETAIL_TRACE_TEMP_RE.fullmatch(value)
    )


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

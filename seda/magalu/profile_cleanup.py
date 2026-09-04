"""통합 수집에서 생성한 Magalu Chromium 프로필의 수명주기를 관리한다.

프로필 삭제는 전용 루트 바로 아래의 실행 시각 기반 이름만 허용한다. 삭제 전
디렉터리를 tombstone으로 원자 이동해 실행 중이거나 잠긴 프로필을 부분 삭제하지
않으며, 링크·junction을 따라가지 않는다.
"""

import argparse
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path


_PROFILE_NAME_RE = re.compile(
    r"^(?P<series>run_magalu_[A-Za-z0-9_]+_)"
    r"\d{8}_\d{6}(?:_w\d+)?$"
)
_TOMBSTONE_NAME_RE = re.compile(
    r"^(?P<profile>run_magalu_[A-Za-z0-9_]+_\d{8}_\d{6}(?:_w\d+)?)"
    r"\.\d+\.\d+\.deleting$"
)
_WORKER_SUFFIX_RE = re.compile(r"^_w\d+$")
_TRUE_VALUES = {"1", "true", "yes", "y"}


def main(argv=None):
    """prepare 또는 finalize 정리를 실행하고 안전한 요약을 출력한다."""
    parser = argparse.ArgumentParser(
        description="Clean isolated Magalu browser profiles safely."
    )
    parser.add_argument("mode", choices=("prepare", "finalize"))
    args = parser.parse_args(argv)

    if not _env_enabled("SEDA_MAGALU_PROFILE_CLEANUP", "1"):
        print(
            "[seda][storage] magalu profile cleanup disabled",
            flush=True,
        )
        return 0

    try:
        result = prepare() if args.mode == "prepare" else finalize()
    except Exception as exc:
        print(
            "[seda][storage] "
            f"mode={args.mode} success=0 error={type(exc).__name__}:{exc}",
            flush=True,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result.get("success") else 1


def prepare(*, now=None, disk_usage_func=None):
    """오래된 프로필과 tombstone을 정리한 뒤 디스크 여유 공간을 확인한다."""
    root, current = _profile_paths()
    root.mkdir(parents=True, exist_ok=True)
    _assert_safe_root(root)
    stale_result = cleanup_stale_profiles(root, current=current, now=now)
    disk_result = disk_capacity(root, disk_usage_func=disk_usage_func)
    success = disk_result["sufficient"]
    return {
        "success": success,
        "mode": "prepare",
        "stale_deleted": stale_result["deleted"],
        "tombstones_deleted": stale_result["tombstones_deleted"],
        "freed_bytes": stale_result["freed_bytes"],
        "cleanup_errors": stale_result["errors"],
        **disk_result,
    }


def finalize(*, disk_usage_func=None):
    """통합 BAT가 사용한 현재 base·worker 프로필만 제거한다."""
    root, current = _profile_paths()
    if not root.exists():
        return {
            "success": True,
            "mode": "finalize",
            "deleted": [],
            "tombstones_deleted": [],
            "freed_bytes": 0,
            "cleanup_errors": [],
            "cleanup_warnings": [],
        }
    _assert_safe_root(root)
    tombstone_result = _cleanup_tombstones(root, current=current)
    deleted = []
    errors = []
    freed_bytes = tombstone_result["freed_bytes"]
    for path in _current_profile_family(root, current):
        try:
            freed_bytes += _remove_profile_directory(path, root)
            deleted.append(str(path))
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            errors.append(_error_item(path, exc))
    disk_result = disk_capacity(root, disk_usage_func=disk_usage_func)
    return {
        "success": not errors,
        "mode": "finalize",
        "deleted": deleted,
        "tombstones_deleted": tombstone_result["deleted"],
        "freed_bytes": freed_bytes,
        "cleanup_errors": errors,
        "cleanup_warnings": tombstone_result["errors"],
        **disk_result,
    }


def cleanup_stale_profiles(root, *, current, now=None):
    """현재 실행을 제외하고 보존 시간이 지난 실행형 프로필만 제거한다."""
    root = Path(root)
    current = Path(current)
    retention_hours = _env_non_negative_float(
        "SEDA_MAGALU_PROFILE_RETENTION_HOURS",
        "48",
    )
    cutoff = float(time.time() if now is None else now) - retention_hours * 3600
    current_names = {
        path.name for path in _current_profile_family(root, current, existing_only=False)
    }
    current_series = _profile_series(current.name)
    if not current_series:
        raise RuntimeError("profile_name_not_managed")
    tombstone_result = _cleanup_tombstones(root, current=current)
    deleted = []
    errors = list(tombstone_result["errors"])
    freed_bytes = tombstone_result["freed_bytes"]

    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == ".seda_profile_cleanup":
            continue
        if _is_reparse_point(path):
            errors.append({"path": str(path), "error": "reparse_point"})
            continue
        if (
            not path.is_dir()
            or not _PROFILE_NAME_RE.fullmatch(path.name)
            or _profile_series(path.name) != current_series
            or path.name in current_names
        ):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            freed_bytes += _remove_profile_directory(path, root)
            deleted.append(str(path))
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            errors.append(_error_item(path, exc))

    return {
        "deleted": deleted,
        "tombstones_deleted": tombstone_result["deleted"],
        "freed_bytes": freed_bytes,
        "errors": errors,
    }


def disk_capacity(path, *, disk_usage_func=None):
    """프로필 드라이브의 여유 공간이 설정된 최소값 이상인지 반환한다."""
    minimum_gb = _env_non_negative_float("SEDA_STORAGE_MIN_FREE_GB", "2")
    usage = (disk_usage_func or shutil.disk_usage)(path)
    required_bytes = int(minimum_gb * 1024**3)
    return {
        "free_bytes": int(usage.free),
        "free_gb": round(int(usage.free) / 1024**3, 2),
        "required_free_bytes": required_bytes,
        "required_free_gb": minimum_gb,
        "sufficient": int(usage.free) >= required_bytes,
    }


def _profile_paths():
    raw_root = Path(
        os.path.abspath(
            os.getenv("SEDA_MAGALU_PROFILE_ROOT", "C:/tmp/seda_magalu_profiles")
        )
    )
    _assert_safe_root(raw_root)
    root = raw_root.resolve()
    raw_current = os.getenv("SEDA_MAGALU_BROWSER_PROFILE", "").strip()
    if not raw_current:
        raise RuntimeError("missing_SEDA_MAGALU_BROWSER_PROFILE")
    current = Path(os.path.abspath(raw_current)).resolve()
    if current.parent != root:
        raise RuntimeError("profile_path_outside_managed_root")
    if not _PROFILE_NAME_RE.fullmatch(current.name):
        raise RuntimeError("profile_name_not_managed")
    return root, current


def _current_profile_family(root, current, *, existing_only=True):
    root = Path(root)
    current = Path(current)
    candidates = [root / current.name]
    if root.is_dir():
        candidates.extend(
            path
            for path in root.iterdir()
            if path.name.startswith(current.name)
            and _WORKER_SUFFIX_RE.fullmatch(path.name[len(current.name) :])
        )
    unique = {path.name: path for path in candidates}
    result = [unique[name] for name in sorted(unique)]
    if existing_only:
        result = [path for path in result if path.is_dir()]
    return result


def _remove_profile_directory(path, root):
    path = Path(path)
    root = Path(root)
    _assert_direct_managed_child(path, root)
    _assert_tree_has_no_reparse_points(path)
    tombstone_root = root / ".seda_profile_cleanup"
    tombstone_root.mkdir(parents=True, exist_ok=True)
    _assert_direct_child(tombstone_root, root)
    tombstone = tombstone_root / (
        f"{path.name}.{os.getpid()}.{time.time_ns()}.deleting"
    )
    attempts = max(1, _env_non_negative_int("SEDA_PROFILE_DELETE_ATTEMPTS", "5"))
    delay_seconds = _env_non_negative_float(
        "SEDA_PROFILE_DELETE_RETRY_SECONDS",
        "1",
    )
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            os.replace(path, tombstone)
            break
        except FileNotFoundError:
            raise
        except OSError as exc:
            last_error = exc
            if attempt < attempts and delay_seconds:
                time.sleep(delay_seconds)
    else:
        raise last_error
    return _remove_tree_without_reparse_points(tombstone)


def _cleanup_tombstones(root, *, current):
    tombstone_root = Path(root) / ".seda_profile_cleanup"
    result = {"deleted": [], "freed_bytes": 0, "errors": []}
    if not tombstone_root.exists():
        return result
    _assert_direct_child(tombstone_root, root)
    if _is_reparse_point(tombstone_root):
        result["errors"].append(
            {"path": str(tombstone_root), "error": "reparse_point"}
        )
        return result
    current_series = _profile_series(Path(current).name)
    if not current_series:
        raise RuntimeError("profile_name_not_managed")
    for path in sorted(tombstone_root.iterdir(), key=lambda item: item.name):
        match = _TOMBSTONE_NAME_RE.fullmatch(path.name)
        if not path.is_dir() or not match or _is_reparse_point(path):
            continue
        if _profile_series(match.group("profile")) != current_series:
            continue
        try:
            result["freed_bytes"] += _remove_tree_without_reparse_points(path)
            result["deleted"].append(str(path))
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            result["errors"].append(_error_item(path, exc))
    try:
        tombstone_root.rmdir()
    except (FileNotFoundError, OSError):
        pass
    return result


def _assert_safe_root(root):
    root = Path(root)
    if _is_reparse_point(root):
        raise RuntimeError("profile_root_reparse_point")
    if root.parent == root:
        raise RuntimeError("profile_root_too_broad")
    if root.name.casefold() != "seda_magalu_profiles":
        raise RuntimeError("profile_root_name_not_managed")


def _profile_series(name):
    match = _PROFILE_NAME_RE.fullmatch(str(name))
    return match.group("series") if match else ""


def _assert_direct_managed_child(path, root):
    _assert_direct_child(path, root)
    if not _PROFILE_NAME_RE.fullmatch(Path(path).name):
        raise RuntimeError("profile_name_not_managed")


def _assert_direct_child(path, root):
    resolved_root = Path(root).resolve()
    resolved_path = Path(path).resolve()
    if resolved_path.parent != resolved_root:
        raise RuntimeError("profile_path_outside_managed_root")
    if _is_reparse_point(path):
        raise RuntimeError("profile_reparse_point")


def _assert_tree_has_no_reparse_points(root):
    pending = [Path(root)]
    while pending:
        directory = pending.pop()
        if _is_reparse_point(directory):
            raise RuntimeError(f"profile_reparse_point:{directory}")
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _is_reparse_point(path):
                    raise RuntimeError(f"profile_reparse_point:{path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)


def _remove_tree_without_reparse_points(root):
    removed_bytes = 0
    pending = [(Path(root), False)]
    while pending:
        path, visited = pending.pop()
        if _is_reparse_point(path):
            raise RuntimeError(f"profile_reparse_point:{path}")
        if visited:
            try:
                path.rmdir()
            except PermissionError:
                os.chmod(path, stat.S_IWRITE)
                path.rmdir()
            continue
        pending.append((path, True))
        with os.scandir(path) as entries:
            for entry in entries:
                child = Path(entry.path)
                if _is_reparse_point(child):
                    raise RuntimeError(f"profile_reparse_point:{child}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((child, False))
                    continue
                try:
                    removed_bytes += child.stat().st_size
                except OSError:
                    pass
                try:
                    child.unlink()
                except PermissionError:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
    return removed_bytes


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


def _env_enabled(name, default):
    return os.getenv(name, default).strip().lower() in _TRUE_VALUES


def _env_non_negative_float(name, default):
    value = float(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _env_non_negative_int(name, default):
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be zero or greater")
    return value


def _error_item(path, exc):
    return {
        "path": str(path),
        "error": f"{type(exc).__name__}:{exc}",
    }


if __name__ == "__main__":
    raise SystemExit(main())

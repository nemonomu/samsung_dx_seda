"""Crash-recoverable publication of step08 product and trace files."""

import csv
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


TRANSACTION_FILENAME = "detail_publish_transaction.json"
LOCK_FILENAME = "detail_publish.lock"
RUN_LOCK_FILENAME = "detail_run.lock"
LOCK_DIRECTORY_NAME = ".seda_detail_locks"
RESOLVED_STATUSES = {"committed", "rolled_back"}


def transaction_path(root):
    return Path(root) / "detail" / "trace" / TRANSACTION_FILENAME


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_detail_publish_transaction(root):
    path = transaction_path(root)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"detail_publish_transaction_invalid:{type(exc).__name__}:path={path}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"detail_publish_transaction_invalid:not_object:path={path}")
    return value


def recover_detail_publish_transaction(root):
    """Finish or roll back a transaction left in ``prepared`` state."""
    root = Path(root).resolve()
    with detail_publish_lock(root):
        return _recover_detail_publish_transaction_unlocked(root)


def _recover_detail_publish_transaction_unlocked(root):
    journal = read_detail_publish_transaction(root)
    if not journal:
        return {"status": "none"}
    status = str(journal.get("status") or "")
    if status in RESOLVED_STATUSES:
        return journal
    if status != "prepared":
        raise RuntimeError(f"detail_publish_transaction_unresolved:status={status or 'blank'}")

    entries = _validated_journal_entries(root, journal)
    if entries and all(
        entry["canonical"].is_file()
        and file_sha256(entry["canonical"]) == entry["new_sha256"]
        for entry in entries
    ):
        journal["status"] = "committed"
        journal["resolved_at"] = _timestamp()
        journal["recovery"] = "finalized_new_files"
        _write_journal(root, journal)
        return journal

    try:
        _restore_entries(entries)
    except Exception as exc:
        raise RuntimeError(
            f"detail_publish_transaction_unresolved:rollback_failed:{type(exc).__name__}:{exc}"
        ) from exc
    journal["status"] = "rolled_back"
    journal["resolved_at"] = _timestamp()
    journal["recovery"] = "restored_previous_files"
    _write_journal(root, journal)
    return journal


def assert_detail_publish_resolved(root):
    journal = recover_detail_publish_transaction(root)
    status = str(journal.get("status") or "none")
    if status not in {"none", *RESOLVED_STATUSES}:
        raise RuntimeError(f"detail_publish_transaction_unresolved:status={status}")
    return journal


def assert_detail_publish_complete(
    root,
    *,
    recover=True,
    expected_product_path=None,
    expected_target_path=None,
):
    """Require a resolved, final detail snapshot when a journal is present."""
    root = Path(root).resolve()
    expected_product_path = (
        Path(expected_product_path)
        if expected_product_path is not None
        else root / "output" / "final_output_enriched.csv"
    )
    if expected_target_path is None:
        # Match step08: an explicit relative environment path is interpreted
        # from the process working directory, while the default is run-local.
        expected_target_path = Path(
            os.getenv(
                "SEDA_DETAIL_TARGET_CSV",
                str(root / "output" / "seda_final_targets.csv"),
            )
        ).resolve()
    else:
        expected_target_path = Path(expected_target_path)
    if not recover:
        journal = read_detail_publish_transaction(root) or {"status": "none"}
        return _assert_detail_publish_complete_unlocked(
            root,
            journal,
            expected_product_path=expected_product_path,
            expected_target_path=expected_target_path,
        )
    with detail_publish_lock(root):
        journal = _recover_detail_publish_transaction_unlocked(root)
        return _assert_detail_publish_complete_unlocked(
            root,
            journal,
            expected_product_path=expected_product_path,
            expected_target_path=expected_target_path,
        )


@contextmanager
def detail_consumer_guard(root):
    """Keep one complete detail snapshot stable for a downstream consumer.

    The run lock serializes production detail publication, direct downstream
    execution, and full-run cleanup. Completion is checked only after that
    lock is held so no accepted snapshot can be replaced during consumption.
    """
    root = Path(root).resolve()
    with detail_run_lock(root):
        journal = assert_detail_publish_complete(root)
        yield journal


def _assert_detail_publish_complete_unlocked(
    root,
    journal,
    *,
    expected_product_path,
    expected_target_path,
):
    status = str(journal.get("status") or "none")
    if status == "none":
        # Backward compatibility for run roots created before the journal
        # contract existed.  Their ordinary file checks still apply.
        return journal
    error = detail_publish_completion_error(
        root,
        journal,
        expected_product_path=expected_product_path,
        expected_target_path=expected_target_path,
    )
    if error:
        raise RuntimeError(f"detail_publish_incomplete:{error}")
    return journal


def detail_publish_completion_error(
    root,
    journal,
    *,
    expected_product_path=None,
    expected_target_path=None,
):
    """Return why a journal is unsafe for downstream use, or blank when valid."""
    root = Path(root).resolve()
    status = str(journal.get("status") or "none")
    metadata = journal.get("metadata")
    complete = isinstance(metadata, dict) and metadata.get("complete") is True
    try:
        product_rows = int(metadata.get("product_row_count"))
        expected_rows = int(metadata.get("expected_row_count"))
    except (AttributeError, TypeError, ValueError):
        product_rows = -1
        expected_rows = -2
    summary = (
        f"status={status}:complete={int(complete)}:"
        f"rows={product_rows}/{expected_rows}"
    )
    if (
        status != "committed"
        or not complete
        or product_rows < 0
        or product_rows != expected_rows
    ):
        return summary

    target_sha256 = str(metadata.get("target_sha256") or "")
    target_value = str(metadata.get("target_path") or "")
    if not _is_sha256(target_sha256):
        return f"{summary}:target_hash_invalid"
    if not target_value:
        return f"{summary}:target_path_missing"
    target_path = Path(target_value)
    if not target_path.is_absolute():
        target_path = root / target_path
    try:
        target_path = target_path.resolve()
        if expected_target_path is not None:
            expected_target_path = Path(expected_target_path)
            if not expected_target_path.is_absolute():
                expected_target_path = root / expected_target_path
            if target_path != expected_target_path.resolve():
                return f"{summary}:target_path_mismatch"
        if not target_path.is_file():
            return f"{summary}:target_missing"
        if file_sha256(target_path) != target_sha256:
            return f"{summary}:target_hash_mismatch"
        target_identities, target_error = _csv_identity_rows(target_path)
        if target_error:
            return f"{summary}:target_csv_{target_error}"
        if file_sha256(target_path) != target_sha256:
            return f"{summary}:target_hash_mismatch"
    except (OSError, UnicodeError, csv.Error) as exc:
        return f"{summary}:target_unreadable:{type(exc).__name__}"
    if len(target_identities) != expected_rows:
        return f"{summary}:target_csv_rows={len(target_identities)}"

    try:
        entries = _validated_journal_entries(root, journal)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return f"{summary}:journal_files_invalid:{type(exc).__name__}"
    product_entries = [
        entry for entry in entries if str(entry.get("name") or "") == "product"
    ]
    if len(product_entries) != 1:
        return f"{summary}:product_entry_count={len(product_entries)}"
    product = product_entries[0]
    canonical = product["canonical"]
    if expected_product_path is not None:
        expected_product_path = Path(expected_product_path)
        if not expected_product_path.is_absolute():
            expected_product_path = root / expected_product_path
        if canonical != expected_product_path.resolve():
            return f"{summary}:product_path_mismatch"
    try:
        if not canonical.is_file():
            return f"{summary}:product_missing"
        if file_sha256(canonical) != product["new_sha256"]:
            return f"{summary}:product_hash_mismatch"
        product_identities, product_error = _csv_identity_rows(canonical)
        if product_error:
            return f"{summary}:product_csv_{product_error}"
        if file_sha256(canonical) != product["new_sha256"]:
            return f"{summary}:product_hash_mismatch"
        actual_rows = len(product_identities)
    except (OSError, UnicodeError, csv.Error) as exc:
        return f"{summary}:product_unreadable:{type(exc).__name__}"
    if actual_rows != product_rows:
        return f"{summary}:product_csv_rows={actual_rows}"
    if product_identities != target_identities:
        mismatch = next(
            (
                index
                for index, (product_identity, target_identity) in enumerate(
                    zip(product_identities, target_identities),
                    start=1,
                )
                if product_identity != target_identity
            ),
            1,
        )
        return f"{summary}:product_identity_at={mismatch}"
    return ""


def mark_detail_publish_incomplete(
    root,
    *,
    expected_row_count,
    target_sha256="",
    target_path="",
):
    """Invalidate an older completion marker before a new detail run starts."""
    root = Path(root).resolve()
    with detail_publish_lock(root):
        previous = _recover_detail_publish_transaction_unlocked(root)
        journal = dict(previous) if previous.get("status") != "none" else {}
        journal.update(
            {
                "version": 1,
                "status": "committed",
                "kind": "detail_run_marker",
                "run_started_at": _timestamp(),
                "metadata": {
                    "complete": False,
                    "expected_row_count": int(expected_row_count),
                    "target_sha256": str(target_sha256 or ""),
                    "target_path": str(target_path or ""),
                },
            }
        )
        _write_journal(root, journal)
        return journal


def publish_detail_files(root, files, *, run_token=None, metadata=None):
    """Publish staged files in order, recovering all canonicals on any error.

    ``files`` is an ordered iterable of dictionaries with ``name``,
    ``canonical`` and ``staged``.  Callers put traces first and the product CSV
    last.  All files must already have passed their format/content validation.
    """
    root = Path(root).resolve()
    with detail_publish_lock(root):
        _recover_detail_publish_transaction_unlocked(root)
        return _publish_detail_files_unlocked(
            root,
            files,
            run_token=run_token,
            metadata=metadata,
        )


def _publish_detail_files_unlocked(root, files, *, run_token=None, metadata=None):
    token = _safe_token(run_token or f"{os.getpid()}_{time.time_ns()}")
    entries = []
    for index, raw in enumerate(files):
        canonical = _within_root(root, raw["canonical"])
        staged = _within_root(root, raw["staged"])
        if not staged.is_file():
            raise RuntimeError(f"detail_publish_stage_missing:{staged}")
        backup = canonical.with_name(f".{canonical.name}.detail_publish.backup")
        backup = _within_root(root, backup)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        old_exists = canonical.is_file()
        old_sha256 = file_sha256(canonical) if old_exists else ""
        if old_exists:
            _copy_file_atomic(canonical, backup)
            if file_sha256(backup) != old_sha256:
                raise RuntimeError(f"detail_publish_backup_hash_mismatch:{backup}")
        else:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        entries.append(
            {
                "order": index,
                "name": str(raw.get("name") or canonical.name),
                "canonical": canonical,
                "staged": staged,
                "backup": backup,
                "old_exists": old_exists,
                "old_sha256": old_sha256,
                "new_sha256": file_sha256(staged),
            }
        )

    journal = {
        "version": 1,
        "run_token": token,
        "status": "prepared",
        "prepared_at": _timestamp(),
        "metadata": dict(metadata or {}),
        "files": [_serializable_entry(root, entry) for entry in entries],
    }
    _write_journal(root, journal)
    try:
        for entry in entries:
            os.replace(entry["staged"], entry["canonical"])
        for entry in entries:
            actual = file_sha256(entry["canonical"])
            if actual != entry["new_sha256"]:
                raise RuntimeError(
                    f"detail_publish_new_hash_mismatch:{entry['name']}:{actual}"
                )
    except Exception as publish_exc:
        try:
            _restore_entries(entries)
        except Exception as rollback_exc:
            raise RuntimeError(
                "detail_publish_transaction_unresolved:"
                f"publish={type(publish_exc).__name__}:{publish_exc}:"
                f"rollback={type(rollback_exc).__name__}:{rollback_exc}"
            ) from publish_exc
        journal["status"] = "rolled_back"
        journal["resolved_at"] = _timestamp()
        journal["error"] = f"{type(publish_exc).__name__}:{publish_exc}"
        _write_journal(root, journal)
        raise

    journal["status"] = "committed"
    journal["resolved_at"] = _timestamp()
    _write_journal(root, journal)
    return journal


def _validated_journal_entries(root, journal):
    raw_entries = journal.get("files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeError("detail_publish_transaction_unresolved:missing_files")
    entries = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise RuntimeError("detail_publish_transaction_unresolved:invalid_file_entry")
        entry = dict(raw)
        for key in ("canonical", "staged", "backup"):
            entry[key] = _within_root(root, raw.get(key))
        for key in ("new_sha256", "old_sha256"):
            value = str(raw.get(key) or "")
            if key == "new_sha256" and not _is_sha256(value):
                raise RuntimeError(
                    f"detail_publish_transaction_unresolved:invalid_{key}"
                )
            if key == "old_sha256" and value and not _is_sha256(value):
                raise RuntimeError(
                    f"detail_publish_transaction_unresolved:invalid_{key}"
                )
            entry[key] = value
        entry["old_exists"] = bool(raw.get("old_exists"))
        entries.append(entry)
    entries.sort(key=lambda item: int(item.get("order", 0)))
    return entries


def _restore_entries(entries):
    # Keep the product CSV as the last visible commit marker during rollback,
    # just as it is during forward publication.  An unchanged canonical is
    # skipped so a failed product replacement does not needlessly rewrite the
    # still-correct (and possibly locked) old product file.
    for entry in entries:
        canonical = entry["canonical"]
        if entry["old_exists"]:
            try:
                already_restored = (
                    canonical.is_file()
                    and file_sha256(canonical) == entry["old_sha256"]
                )
            except OSError:
                already_restored = False
            if already_restored:
                continue
            backup = entry["backup"]
            if not backup.is_file():
                raise RuntimeError(f"detail_publish_backup_missing:{backup}")
            if file_sha256(backup) != entry["old_sha256"]:
                raise RuntimeError(f"detail_publish_backup_corrupt:{backup}")
            _copy_file_atomic(backup, canonical)
        else:
            if not canonical.exists():
                continue
            try:
                canonical.unlink()
            except FileNotFoundError:
                pass
    for entry in entries:
        canonical = entry["canonical"]
        if entry["old_exists"]:
            if not canonical.is_file() or file_sha256(canonical) != entry["old_sha256"]:
                raise RuntimeError(f"detail_publish_restore_hash_mismatch:{canonical}")
        elif canonical.exists():
            raise RuntimeError(f"detail_publish_restore_expected_absent:{canonical}")


def _copy_file_atomic(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.detail_publish.tmp"
    )
    try:
        with Path(source).open("rb") as source_handle, temp.open("wb") as target:
            shutil.copyfileobj(source_handle, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, destination)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _write_journal(root, value):
    path = transaction_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _serializable_entry(root, entry):
    value = dict(entry)
    for key in ("canonical", "staged", "backup"):
        value[key] = str(value[key].relative_to(root))
    return value


@contextmanager
def detail_publish_lock(root):
    """Hold a run-root lock that the OS releases automatically on process exit."""
    with _detail_file_lock(
        root,
        LOCK_FILENAME,
        error_token="detail_publish_locked",
    ):
        yield


@contextmanager
def detail_run_lock(root):
    """Prevent two parent detail runs from mutating the same run root."""
    with _detail_file_lock(
        root,
        RUN_LOCK_FILENAME,
        error_token="detail_run_locked",
    ):
        yield


@contextmanager
def _detail_file_lock(root, filename, *, error_token):
    root = Path(root).resolve()
    lock_key = hashlib.sha256(
        os.path.normcase(str(root)).encode("utf-8")
    ).hexdigest()[:16]
    lock_label = _safe_token(root.name)[:48]
    path = (
        root.parent
        / LOCK_DIRECTORY_NAME
        / f"{lock_label}.{lock_key}.{filename}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        if os.path.getsize(path) == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"{error_token}:path={path}") from exc
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _within_root(root, value):
    if value in (None, ""):
        raise RuntimeError("detail_publish_path_missing")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"detail_publish_path_outside_root:{resolved}")
    return resolved


def _safe_token(value):
    token = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in str(value or "")
    ).strip("._")
    if not token:
        raise RuntimeError("detail_publish_run_token_blank")
    return token[:160]


def _is_sha256(value):
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _csv_identity_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [
            column for column in ("item", "product_url") if column not in fieldnames
        ]
        if missing:
            return [], f"missing_columns:{','.join(missing)}"
        identities = []
        for index, row in enumerate(reader, start=1):
            if None in row:
                return [], f"extra_values:row={index}"
            if row.get("item") is None or row.get("product_url") is None:
                return [], f"short_row:row={index}"
            identities.append(
                (
                    str(row.get("item") or ""),
                    str(row.get("product_url") or ""),
                )
            )
        return identities, ""


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")

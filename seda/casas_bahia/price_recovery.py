"""Explicit price-only recovery; preserve the original CSV and DB crawl time."""
import argparse
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from seda import step00_config as config
from seda.detail_publish import assert_detail_publish_complete, detail_run_lock
from seda.step14_db_load import _db_value, _validate_db_csv_schema
from seda.step15_final_output import (
    _discount_type_for_output, _price_for_output, _prices_equal,
    _savings_for_output, _validate_source_context,
)
from .detail_price_recovery import PRICE_FIELDS, _identity, _positive_id, is_blank
from .mode3_price_recovery import BrowserPriceFetcher, _recover_without_seller
from .rest_detail_price_recovery import recover_rest_price_fields


IDENTITY_COLUMNS = ("account_name", "product", "item", "sku", "product_url", "crawl_strdatetime", "batch_id")


class RecoveryError(Exception):
    """Only application-authored, non-sensitive error codes belong here."""


def identity_values(row):
    for field in IDENTITY_COLUMNS:
        if field != "sku" and is_blank(row.get(field)):
            raise RecoveryError("missing_db_identity")
    return tuple(_db_value(field, row.get(field)) for field in IDENTITY_COLUMNS)


def table_sql(table):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?", table):
        raise RecoveryError("invalid_table_identifier")
    return ".".join('"' + part + '"' for part in table.split("."))


class PriceStore:
    """Short transactions; never hold DB row locks while Chrome is collecting."""
    def __init__(self, connection, table):
        self.connection = connection
        self.table = table_sql(table)
        self.where = " AND ".join(f'"{field}" IS NOT DISTINCT FROM %s' for field in IDENTITY_COLUMNS)

    def _select(self, cursor, row, lock=False):
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        columns = ", ".join(f'"{field}"' for field in PRICE_FIELDS)
        cursor.execute(f"SELECT {columns} FROM {self.table} WHERE {self.where} LIMIT 2"
                       + (" FOR UPDATE" if lock else ""), identity_values(row))
        records = cursor.fetchall()
        if len(records) != 1:
            return None, "db_row_missing" if not records else "db_row_ambiguous"
        return {f: _db_value(f, v) for f, v in zip(PRICE_FIELDS, records[0])}, ""

    def snapshot(self, row):
        with self.connection:
            with self.connection.cursor() as cursor:
                return self._select(cursor, row)

    def update(self, row, before, changes):
        if not changes or not set(changes) <= set(PRICE_FIELDS):
            raise RecoveryError("invalid_update_fields")
        with self.connection:
            with self.connection.cursor() as cursor:
                current, error = self._select(cursor, row, lock=True)
                if error:
                    return error
                if any(_db_value(f, current[f]) != _db_value(f, before[f]) for f in PRICE_FIELDS):
                    return "db_changed_during_recovery"
                if not is_blank(current["final_sku_price"]):
                    return "db_price_already_present"
                if any(not is_blank(current[f]) for f in changes):
                    return "db_nonempty_field"
                assignments = ", ".join(f'"{f}" = %s' for f in changes)
                cursor.execute(f"UPDATE {self.table} SET {assignments} WHERE {self.where}",
                               tuple(_db_value(f, v) for f, v in changes.items()) + identity_values(row))
                if cursor.rowcount != 1:
                    # Raising here rolls back even if a duplicate appeared after SELECT.
                    raise RecoveryError("db_update_count_mismatch")
        return "updated"  # The connection context has committed successfully.


def save_csv(path, rows, columns):
    """Do not run translation or final formatting over historical non-price fields."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def checkpoint(folder, rows, columns, reports, stats):
    save_csv(folder / "final_output_recovered.csv", rows, columns)
    for name, value in (("results.json", reports), ("summary.json", dict(stats))):
        path = folder / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def price_changes(before, detail):
    values = dict(detail, retailer="Casas Bahia")
    old = _price_for_output(detail.get("original_sku_price"))
    final = _price_for_output(detail.get("final_sku_price"))
    if old and final and _prices_equal(old, final):
        old = ""
    formatted = {"original_sku_price": old, "final_sku_price": final,
                 "savings": _savings_for_output(values),
                 "discount_type": _discount_type_for_output(detail.get("discount_type"))}
    return {f: v for f, v in formatted.items() if is_blank(before.get(f)) and not is_blank(v)}


def _reason(error):
    return {
        "missing_price_offer": "사용 가능한 가격 항목 없음",
        "missing_candidate_seller": "판매자 후보 없음",
        "seller_discovery_unavailable": "판매자 조회 실패",
        "price_sku_identity_conflict": "원래 SKU와 가격 응답 SKU가 다름",
        "price_product_identity_conflict": "상품 번호 불일치",
        "price_seller_identity_conflict": "판매자 불일치",
        "db_row_missing": "기존 DB 행 없음: 새 행을 추가하지 않음",
        "db_row_ambiguous": "기존 DB 행이 여러 건이어서 갱신하지 않음",
        "db_changed_during_recovery": "재조회 중 DB 가격이 변경되어 갱신하지 않음",
        "db_update_count_mismatch": "DB 갱신 행수가 1건이 아니어서 이번 갱신을 취소함",
        "db_price_already_present": "DB에 가격이 이미 있어 갱신하지 않음",
        "db_nonempty_field": "기존 DB 값을 덮어쓰지 않도록 갱신 중단",
        "source_identity_ambiguous": "원래 상품 식별 정보가 없거나 여러 건임",
        "missing_db_identity": "최초 수집 행을 식별할 정보가 부족함",
    }.get(error, "조회 또는 검증 실패: " + error)


def human_log(report):
    success = report["outcome"] in {"updated", "csv_only", "already_present"}
    print("\n[복구 성공]" if success else "\n[사람 확인 필요]", report["reason"], flush=True)
    for label, key in (("상품군", "product"), ("상품명", "name"), ("item", "item"),
                       ("sku", "sku"), ("원래 옵션 번호", "target_sku"),
                       ("원래 상품 URL", "url"), ("최초 수집 시간(유지)", "crawl_strdatetime")):
        print(f"  {label}: {report.get(key, '')}", flush=True)
    if report.get("candidate_skus"):
        print("  판매자 후보 조회에서 반환된 옵션 번호: " + ", ".join(report["candidate_skus"]), flush=True)
    for field, change in report.get("changes", {}).items():
        label = {"original_sku_price": "원가", "final_sku_price": "최종 판매가/결제할인가",
                 "savings": "할인 표시", "discount_type": "할인 조건"}[field]
        print(f"  {label}: {change['before'] or 'NULL'} -> {change['after']}", flush=True)
    if report.get("quoted_prices"):
        print("  응답 가격(검증한 SKU): " + json.dumps(report["quoted_prices"], ensure_ascii=False), flush=True)
    outcome = {"updated": "DB 갱신·커밋 완료", "csv_only": "복구 CSV 저장, DB 갱신 안 함",
               "already_present": "DB 기존 가격 유지, 재조회 안 함", "review": "복구 못함, DB 갱신 없음",
               "db_update_failed": "가격 확인 성공, DB 갱신 실패(재실행 시 DB 상태 재확인)"}.get(
                   report["outcome"], "DB 갱신하지 않음")
    print(f"  처리 결과: {outcome} / 페이지 존재 여부: 미확인", flush=True)
    if not success:
        print("  위 URL에서 원래 옵션을 선택하고 판매자·가격을 확인해 주세요.", flush=True)


def recover_rows(rows, targets, folder, *, store=None, fetcher=None):
    """Injected store/fetcher allow offline testing with no live DB or browser."""
    folder = Path(folder)
    columns = list(rows[0])
    output = [dict(row) for row in rows]
    stats, reports, cache = Counter(), [], {}
    source_by_url = defaultdict(list)
    for source in targets:
        source_by_url[source.get("product_url")].append(source)
    owned = BrowserPriceFetcher() if fetcher is None else None
    fetcher = owned if owned is not None else fetcher
    # No HTTP request occurs until a validated, NULL-priced row actually needs it.
    checkpoint(folder, output, columns, reports, stats)
    try:
        for index, original in enumerate(rows):
            if not is_blank(original.get("final_sku_price")):
                stats["nonempty_skipped"] += 1
                continue
            stats["targets"] += 1
            report = {"row": index + 1, "product": original.get("product"),
                      "name": original.get("retailer_sku_name"), "item": original.get("item"),
                      "sku": original.get("sku"), "url": original.get("product_url"),
                      "crawl_strdatetime": original.get("crawl_strdatetime"),
                      "recovered_at": datetime.now().isoformat(timespec="seconds"),
                      "outcome": "review", "reason": "", "changes": {}}
            print(f"[진행] 가격 복구 {stats['targets']} / 원본 행 {index + 1}/{len(rows)}", flush=True)
            try:
                identity_values(original)
                before, error = (store.snapshot(original) if store else
                                 ({f: original.get(f, "") for f in PRICE_FIELDS}, ""))
                if error:
                    raise RecoveryError(error)
                # Reflect only matching DB price fields; never modify historical metadata.
                for field in PRICE_FIELDS:
                    output[index][field] = before[field] if before[field] is not None else ""
                if not is_blank(before["final_sku_price"]):
                    report.update(outcome="already_present", reason="DB에 가격이 있어 재조회하지 않음")
                else:
                    sources = source_by_url.get(original["product_url"], [])
                    if len(sources) != 1:
                        raise RecoveryError("source_identity_ambiguous")
                    work = dict(sources[0], **before)
                    identity, error = _identity(work)
                    if error:
                        raise RecoveryError(error)
                    product, sku, seller = identity
                    report["target_sku"] = sku
                    if work.get("retailer") != "Casas Bahia" or work.get("product_line") != original["product"]:
                        raise RecoveryError("source_context_mismatch")
                    diagnostics = getattr(fetcher, "request_diagnostics", None)
                    start = len(diagnostics) if isinstance(diagnostics, list) else 0
                    result = (recover_rest_price_fields(work, cache=cache, fetcher=fetcher) if seller else
                              _recover_without_seller(work, cache, fetcher))
                    if isinstance(diagnostics, list):
                        report["request_diagnostics"] = diagnostics[start:]
                    report["evidence"] = result.get("evidence", {})
                    report["candidate_skus"] = sorted({
                        _positive_id(o.get("skuId")) for o in cache.get(("mode3_seller_candidates", product), {}).get("offers", [])
                        if isinstance(o, dict) and _positive_id(o.get("productId")) == product and _positive_id(o.get("skuId"))})
                    if not result["verified"]:
                        error = result["error"]
                        report["reason"] = _reason(error)
                        if report["candidate_skus"] and sku not in report["candidate_skus"]:
                            report["reason"] += " / 다른 SKU 가격만 후보 조회됨(옵션명은 직접 확인)"
                        report["error"] = error
                    else:
                        changes = price_changes(before, result["detail"])
                        if "final_sku_price" not in changes:
                            raise RecoveryError("verified_final_price_missing")
                        stats["price_verified"] += 1
                        report["price_verified"] = True
                        resolved_seller = _positive_id(result["evidence"].get("seller_id"))
                        for quote in cache.get((product, sku, resolved_seller), {}).get("offers", []):
                            if (quote.get("productId"), quote.get("skuId"), quote.get("sellerId")) == (product, sku, resolved_seller):
                                from .detail_price_recovery import _number
                                report["quoted_prices"] = {label: str(_number(quote.get(key))) if _number(quote.get(key)) is not None else None
                                    for label, key in (("원가", "oldPrice"), ("일반 판매가", "standardPrice"), ("최종 결제가격", "currentPrice"))}
                                break
                        report["changes"] = {f: {"before": before[f], "after": v} for f, v in changes.items()}
                        output[index].update(changes)
                        # Durable proposed CSV before any DB update. Read it back for the DB values.
                        checkpoint(folder, output, columns, reports + [dict(report, outcome="prepared")], stats)
                        saved = config.read_csv(folder / "final_output_recovered.csv")[index]
                        changes = {f: saved[f] for f in changes}
                        outcome = store.update(original, before, changes) if store else "csv_only"
                        report.update(outcome=outcome, reason="가격 복구 및 DB 커밋 완료" if outcome == "updated" else
                                      "CSV에만 복구(DB 갱신 안 함)" if outcome == "csv_only" else _reason(outcome))
            except Exception as exc:
                # Never log driver/SQL exception messages (which may contain credentials).
                error = str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__
                report.update(outcome="db_update_failed" if report.get("price_verified") and store else "review",
                              error=error, reason=_reason(error))
            stats[report["outcome"]] += 1
            reports.append(report)
            human_log(report)
            checkpoint(folder, output, columns, reports, stats)
    finally:
        if owned is not None:
            owned.close()
    checkpoint(folder, output, columns, reports, stats)
    unresolved = sum(value for key, value in stats.items() if key not in {
        "targets", "nonempty_skipped", "price_verified", "updated", "csv_only", "already_present"})
    print(f"\n[복구 종료] 대상 {stats['targets']}건 / 가격 검증 성공 {stats['price_verified']}건 / "
          f"DB 갱신 완료 {stats['updated']}건 / DB 기존 가격 유지 {stats['already_present']}건 / "
          f"CSV만 복구 {stats['csv_only']}건 / 사람 확인 필요 {unresolved}건", flush=True)
    print(f"복구 결과 CSV: {folder / 'final_output_recovered.csv'}", flush=True)
    return stats, reports


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        self.console.write(text)
        self.console.flush()
        return len(text)

    def flush(self):
        self.log.flush()
        self.console.flush()


def available_batches(line=None):
    """Read only canonical final CSVs; do not list recovery copies as new runs."""
    batches = []
    lines = (line,) if line in {"TV", "REF", "LDY"} else ("TV", "REF", "LDY")
    for selected in lines:
        base = config.DEFAULT_RUNS_BASE / "casas_bahia" / selected.lower()
        for path in base.glob("*/output/final_output.csv"):
            date = path.parent.parent.name
            if not re.fullmatch(r"\d{8}", date):
                continue
            try:
                datetime.strptime(date, "%Y%m%d")
                rows = config.read_csv(path)
            except (OSError, UnicodeError, csv.Error, ValueError):
                continue
            groups = defaultdict(list)
            for row in rows:
                account = re.sub(r"[^a-z]", "", str(row.get("account_name") or "").lower())
                if account not in {"casasbahia", "casasbahiacombr"} or row.get("product") != selected:
                    continue
                batch, crawl = row.get("batch_id"), row.get("crawl_strdatetime")
                if batch and crawl:
                    groups[(batch, crawl)].append(row)
            for (batch, crawl), group in groups.items():
                missing = sum(is_blank(row.get("final_sku_price")) for row in group)
                if missing:
                    batches.append({"line": selected, "date": date, "batch_id": batch,
                                    "crawl_strdatetime": crawl, "missing": missing})
    return sorted(batches, key=lambda b: (b["crawl_strdatetime"], b["date"], b["line"], b["batch_id"]), reverse=True)


def choose_batch(line=None, *, csv_only=False):
    batches = available_batches(line)
    if not batches:
        print("저장된 최종 CSV에서 가격 복구 대상 배치를 찾지 못했습니다.", flush=True)
        return None
    action = "복구 CSV만 저장합니다(DB 변경 없음)." if csv_only else "바로 복구 후 DB에 반영합니다."
    print("어느 수집 결과의 가격을 복구할까요? 번호를 선택하면 " + action, flush=True)
    print("번호 | 상품군 | 최초 수집 시각 | 배치 ID | 판매가 빈 항목(CSV 기준)", flush=True)
    for index, batch in enumerate(batches, 1):
        print(f"{index} | {batch['line']} | {batch['crawl_strdatetime']} | {batch['batch_id']} | {batch['missing']}건", flush=True)
    print("0 | 취소", flush=True)
    while True:
        try:
            choice = input("복구할 번호: ").strip()
        except EOFError:
            return None
        if choice == "0":
            return None
        if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(batches):
            return batches[int(choice) - 1]
        print("목록에 있는 번호를 입력해 주세요.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Casas Bahia 가격 복구: 최초 수집 시각 유지, 기존 행 UPDATE만 수행")
    parser.add_argument("product_line", nargs="?", choices=("TV", "REF", "LDY", "ALL"))
    parser.add_argument("date", nargs="?", help="원래 수집 폴더 날짜 YYYYMMDD")
    parser.add_argument("--csv-only", action="store_true", help="가격 조회 후 별도 CSV만 저장, DB 접속 안 함")
    args = parser.parse_args(argv)
    selection = None
    if not args.date:
        selection = choose_batch(args.product_line, csv_only=args.csv_only)
        if selection is None:
            return 0
        line, date = selection["line"], selection["date"]
    else:
        line, date = args.product_line, args.date
    if line not in {"TV", "REF", "LDY", "ALL"} or not re.fullmatch(r"\d{8}", date):
        parser.error("상품군과 YYYYMMDD 날짜를 확인하세요.")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        parser.error("실제 달력에 있는 날짜를 입력하세요.")
    config.load_env()
    os.environ.update(SEDA_CASAS_BAHIA_LISTING_MODE="3", SEDA_ACTIVE_RETAILER="casas_bahia",
                      SEDA_RETAILERS="casas_bahia", SEDA_POSTAL_CODE="01010-010", SEDA_CASAS_BAHIA_ZIPCODE="01010010")
    failed = False
    for selected in (("TV", "REF", "LDY") if line == "ALL" else (line,)):
        os.environ.update(SEDA_PRODUCT_LINE=selected, SEDA_RUN_DATE=date)
        root = config.dated_run_root("casas_bahia", date, selected)
        os.environ["SEDA_RUN_ROOT"] = str(root)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = config.PROJECT_ROOT / "seda" / "casas_bahia" / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"casas_bahia_price_recovery_{selected}_{timestamp}.log"
        if log_path.exists():
            log_path = log_path.with_stem(log_path.stem + "_" + uuid4().hex[:8])
        with log_path.open("x", encoding="utf-8") as handle, redirect_stdout(Tee(sys.stdout, handle)), redirect_stderr(Tee(sys.stderr, handle)):
            print(f"진단 로그: {log_path}\n대상 수집 날짜: {date} / 상품군: {selected} / mode=3 / CSV only={args.csv_only}", flush=True)
            connection = None
            try:
                with detail_run_lock(root):
                    assert_detail_publish_complete(root, recover=False, expected_target_path=root / "output" / "seda_final_targets.csv")
                    path = root / "output" / "final_output.csv"
                    print(f"원본 CSV: {path}", flush=True)
                    rows = config.read_csv(path)
                    _validate_source_context(rows, path)
                    _validate_db_csv_schema(rows, path)
                    if selection is not None:
                        rows = [row for row in rows if row.get("batch_id") == selection["batch_id"]
                                and row.get("crawl_strdatetime") == selection["crawl_strdatetime"]]
                        if not rows:
                            raise RecoveryError("selected_batch_no_longer_available")
                        print(f"선택 배치 ID: {selection['batch_id']} / 최초 수집 시각: {selection['crawl_strdatetime']}", flush=True)
                    targets = config.read_csv(root / "output" / "seda_final_targets.csv")
                    if not targets:
                        raise RecoveryError("source_targets_missing")
                    folder = root / "recovery" / "price" / (timestamp + "_" + uuid4().hex[:8])
                    folder.mkdir(parents=True, exist_ok=False)
                    save_csv(folder / "final_output_before.csv", rows, list(rows[0]))
                    store = None
                    if not args.csv_only:
                        connection = config.db_connect()
                        store = PriceStore(connection, config.output_table())
                    stats, _ = recover_rows(rows, targets, folder, store=store)
                    failed |= any(stats[k] for k in stats if k not in {
                        "targets", "price_verified", "updated", "csv_only", "already_present", "nonempty_skipped"})
            except Exception as exc:
                error = str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__
                print(f"[실행 실패] {error} / 입력 파일·상품군·DB 설정·동시 실행 여부를 확인하세요.", flush=True)
                failed = True
            finally:
                if connection is not None:
                    connection.close()
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

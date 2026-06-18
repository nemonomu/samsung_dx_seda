import os
import smtplib
from email.message import EmailMessage

from .step00_config import product_line, csv_count, read_json, run_root, write_json


def _send_email(subject, body):
    if os.getenv("SEDA_EMAIL_NOTIFY", "0").lower() not in {"1", "true", "yes", "y"}:
        return "disabled"
    if os.getenv("SEDA_EMAIL_DRY_RUN", "1").lower() in {"1", "true", "yes", "y"}:
        return "dry_run"
    required = ["SEDA_SMTP_SERVER", "SEDA_EMAIL_FROM", "SEDA_EMAIL_PASSWORD", "SEDA_EMAIL_TO"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        return f"missing:{','.join(missing)}"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SEDA_EMAIL_FROM")
    message["To"] = os.getenv("SEDA_EMAIL_TO")
    message.set_content(body)

    server = os.getenv("SEDA_SMTP_SERVER")
    port = int(os.getenv("SEDA_SMTP_PORT", "587"))
    with smtplib.SMTP(server, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(os.getenv("SEDA_EMAIL_FROM"), os.getenv("SEDA_EMAIL_PASSWORD"))
        smtp.send_message(message)
    return "sent"


def _retailer_report_label():
    active = os.getenv("SEDA_ACTIVE_RETAILER") or os.getenv("SEDA_RETAILERS", "")
    key = active.split(",", 1)[0].strip().lower()
    labels = {
        "casas_bahia": "CasasBahia",
        "magalu": "Magalu",
    }
    if key in labels:
        return labels[key]
    if not key:
        return "SEDA"
    return "".join(part.capitalize() for part in key.replace("-", "_").split("_") if part)


def _email_subject(status):
    prefix = "" if status.get("data_success") else "WARNING "
    return f"{prefix}[SEDA] {_retailer_report_label()} {product_line()} crawling report"


def _format_success(value):
    return "SUCCESS" if value else "CHECK NEEDED"


def _append_if(lines, label, value):
    if value not in (None, "", {}):
        lines.append(f"- {label}: {value}")


def _run_date_from_status(status):
    run_root_value = str(status.get("run_root") or "").rstrip("\\/")
    return os.path.basename(run_root_value) if run_root_value else ""


def _build_email_body(status):
    db_prepare = status.get("db_prepare") if isinstance(status.get("db_prepare"), dict) else {}
    db_load = status.get("db_load") if isinstance(status.get("db_load"), dict) else {}
    s3_sync = status.get("s3_sync") if isinstance(status.get("s3_sync"), dict) else {}

    final_rows = int(status.get("final_output_rows") or 0)
    inserted_rows = int(status.get("db_inserted_rows") or 0)
    table = db_load.get("table") or db_prepare.get("table") or ""

    lines = [
        f"[SEDA] {_retailer_report_label()} {product_line()} crawling report",
        "",
        f"Status: {_format_success(status.get('data_success'))}",
        f"Product line: {product_line()}",
        f"Run date: {_run_date_from_status(status)}",
        "",
        "Rows:",
        f"- Main listing: {status.get('main_rows')}",
        f"- Main targets: {status.get('main_target_rows')}",
        f"- BSR listing: {status.get('bsr_rows')}",
        f"- Final targets: {status.get('final_target_rows')}",
        f"- Final output: {final_rows}",
        f"- DB inserted: {inserted_rows}",
        "",
        "DB:",
    ]
    _append_if(lines, "Table", table)
    lines.append(f"- Load status: {_format_success(db_load.get('success') is True)}")

    issues = []
    if final_rows <= 0:
        issues.append("Final output row count is 0.")
    if db_load.get("success") is not True:
        issues.append("DB load did not report success.")
    if inserted_rows != final_rows:
        issues.append(f"DB inserted rows mismatch: inserted={inserted_rows}, final_output={final_rows}.")
    if s3_sync and s3_sync.get("success") is False:
        issues.append(f"S3 sync failed: {s3_sync.get('error') or s3_sync.get('skip_reason') or 'unknown reason'}")

    if issues:
        lines.extend(["", "Issues:"])
        lines.extend(f"- {issue}" for issue in issues)

    return "\n".join(lines).rstrip() + "\n"


def main():
    root = run_root()
    status = {
        "run_root": str(root),
        "product_line": product_line(),
        "main_rows": csv_count(root / "main" / "parsed" / "main_occurrences.csv"),
        "main_target_rows": csv_count(root / "output" / "seda_main_targets.csv"),
        "bsr_rows": csv_count(root / "bsr" / "parsed" / "main_occurrences.csv"),
        "final_target_rows": csv_count(root / "output" / "seda_final_targets.csv"),
        "final_output_rows": csv_count(root / "output" / "final_output.csv"),
        "review20_rows": csv_count(root / "detail" / "parsed" / "review20_rows.csv"),
        "db_prepare": read_json(root / "db" / "manifest_db_prepare.json"),
        "db_load": read_json(root / "db" / "manifest_db_load.json"),
        "s3_sync": read_json(root / "s3" / "manifest_s3_sync.json"),
        "main_manifest": read_json(root / "main" / "manifest.json"),
        "bsr_manifest": read_json(root / "bsr" / "manifest.json"),
    }
    db_load = status.get("db_load") or {}
    status["db_inserted_rows"] = int((db_load.get("inserted") or 0) if isinstance(db_load, dict) else 0)
    status["data_success"] = (
        status["final_output_rows"] > 0
        and db_load.get("success") is True
        and status["db_inserted_rows"] == status["final_output_rows"]
    )
    status["success"] = status["data_success"]
    output = root / "status" / "status_summary.json"
    write_json(output, status)

    body = _build_email_body(status)
    subject = _email_subject(status)
    (root / "status" / "email_report.txt").write_text(body, encoding="utf-8")
    try:
        email_status = _send_email(subject, body)
    except Exception as exc:
        email_status = f"failed:{type(exc).__name__}: {exc}"
    status["email_status"] = email_status
    status["email_subject"] = subject
    status["success"] = status["data_success"] and email_status == "sent"
    write_json(output, status)
    print(f"[seda] wrote {output} email={email_status}")
    if not status["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

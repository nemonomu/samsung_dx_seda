import os
import smtplib
from email.message import EmailMessage

from .step00_config import csv_count, read_json, run_root, write_json


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


def main():
    root = run_root()
    status = {
        "run_root": str(root),
        "main_rows": csv_count(root / "main" / "parsed" / "main_occurrences.csv"),
        "main_target_rows": csv_count(root / "output" / "seda_main_targets.csv"),
        "bsr_rows": csv_count(root / "bsr" / "parsed" / "main_occurrences.csv"),
        "final_target_rows": csv_count(root / "output" / "seda_final_targets.csv"),
        "final_output_rows": csv_count(root / "output" / "final_output.csv"),
        "review20_rows": csv_count(root / "detail" / "parsed" / "review20_rows.csv"),
        "main_manifest": read_json(root / "main" / "manifest.json"),
        "bsr_manifest": read_json(root / "bsr" / "manifest.json"),
    }
    status["success"] = status["final_output_rows"] > 0
    output = root / "status" / "status_summary.json"
    write_json(output, status)

    body = "\n".join(f"{key}: {value}" for key, value in status.items() if key not in {"main_manifest", "bsr_manifest"})
    email_status = _send_email(f"SEDA crawler status - {'success' if status['success'] else 'check needed'}", body)
    status["email_status"] = email_status
    write_json(output, status)
    print(f"[seda] wrote {output} email={email_status}")


if __name__ == "__main__":
    main()

"""Gmail SMTP email delivery for daily scan reports.

Uses stdlib smtplib + email.mime — no extra dependencies.
Credentials come from environment variables:
  GMAIL_ADDRESS       — sender Gmail address
  GMAIL_APP_PASSWORD  — Gmail App Password (not regular password)
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def build_email(
    from_addr: str,
    to_addr: str,
    subject: str,
    report_md_path: Path,
    slate_json_path: Path | None = None,
) -> MIMEMultipart:
    """Build a MIME email with the report as body and slate as attachment.

    Args:
        from_addr: Sender email address.
        to_addr: Recipient email address.
        subject: Email subject line.
        report_md_path: Path to REPORT.md (used as email body).
        slate_json_path: Optional path to DAILY_SLATE.json (attached).

    Returns:
        A fully constructed MIMEMultipart message.
    """
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject

    # Body = report markdown as plain text
    report_text = report_md_path.read_text(encoding="utf-8")
    msg.attach(MIMEText(report_text, "plain", "utf-8"))

    # Attach JSON slate if provided
    if slate_json_path and slate_json_path.exists():
        json_data = slate_json_path.read_bytes()
        attachment = MIMEApplication(json_data, _subtype="json")
        attachment.add_header(
            "Content-Disposition", "attachment", filename=slate_json_path.name,
        )
        msg.attach(attachment)

    return msg


def send_report_email(
    to_addr: str,
    subject: str,
    report_md_path: Path,
    slate_json_path: Path | None = None,
    gmail_address: str | None = None,
    gmail_app_password: str | None = None,
) -> None:
    """Send the daily scan report via Gmail SMTP.

    Args:
        to_addr: Recipient email address.
        subject: Email subject line.
        report_md_path: Path to REPORT.md file.
        slate_json_path: Optional path to DAILY_SLATE.json file.
        gmail_address: Sender Gmail address (or from GMAIL_ADDRESS env var).
        gmail_app_password: Gmail App Password (or from GMAIL_APP_PASSWORD env var).

    Raises:
        ValueError: If credentials are missing.
        smtplib.SMTPException: If email delivery fails.
    """
    import os

    from_addr = gmail_address or os.environ.get("GMAIL_ADDRESS", "")
    password = gmail_app_password or os.environ.get("GMAIL_APP_PASSWORD", "")

    if not from_addr:
        raise ValueError("GMAIL_ADDRESS not set — cannot send email")
    if not password:
        raise ValueError("GMAIL_APP_PASSWORD not set — cannot send email")

    msg = build_email(from_addr, to_addr, subject, report_md_path, slate_json_path)

    logger.info("Sending report email to %s via %s:%d", to_addr, SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(from_addr, password)
        server.send_message(msg)

    logger.info("Email sent successfully")

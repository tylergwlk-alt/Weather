# src/kalshi_weather/spike_alerter.py
"""Spike Alert Emailer — HTML emails with color-coded trading signals.

Sends to self (Gmail to Gmail) with inline CSS for mobile compatibility.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ── Signal color mapping ─────────────────────────────────────────────

_SIGNAL_COLORS: dict[str, str] = {
    "STRONG_BUY": "#22c55e",
    "BUY": "#22c55e",
    "HOLD": "#eab308",
    "CAUTION": "#ef4444",
    "NO_EDGE": "#ef4444",
}


def signal_to_color(signal: str) -> tuple[str, str]:
    """Return (hex_color, label) for a signal string."""
    color = _SIGNAL_COLORS.get(signal, "#6b7280")
    return color, signal


# ── Conviction trend rows ────────────────────────────────────────────


def build_conviction_row(
    index: int,
    total: int,
    time_str: str,
    signal: Optional[str],
    temp_f: Optional[float],
    market_price: Optional[int],
    is_current: bool,
) -> str:
    """Build one row of the conviction trend table."""
    marker = " \u2190 you are here" if is_current else ""
    if signal is None:
        return (
            '<tr style="color:#9ca3af;">'
            f"<td>[{index}/{total}]</td>"
            f"<td>{time_str}</td>"
            f"<td>(pending)</td>"
            f"<td></td>"
            f"<td></td>"
            f"</tr>"
        )
    color, _ = signal_to_color(signal)
    return (
        f"<tr>"
        f"<td>[{index}/{total}]</td>"
        f"<td>{time_str}</td>"
        f'<td style="color:{color};font-weight:bold;">{signal}</td>'
        f"<td>{temp_f:.1f}\u00b0F</td>"
        f"<td>{market_price}\u00a2{marker}</td>"
        f"</tr>"
    )


# ── Full HTML email builder ──────────────────────────────────────────


def build_spike_email_html(
    city: str,
    bracket: str,
    email_number: int,
    email_total: int,
    time_str: str,
    old_price: int,
    new_price: int,
    current_price: int,
    spike_delta: int,
    metar_f: Optional[int],
    precise_f: Optional[float],
    precise_c: Optional[float],
    precise_source: str,
    running_max_f: Optional[int],
    margin_c: Optional[float],
    margin_status: str,
    signal: str,
    signal_reason: str,
    time_risk: str,
    conviction_rows: list[str],
) -> str:
    """Build the full HTML email body."""
    color, label = signal_to_color(signal)

    metar_str = f"{metar_f}\u00b0F" if metar_f is not None else "\u2014"
    precise_f_str = f"{precise_f:.1f}\u00b0F" if precise_f is not None else "\u2014"
    precise_c_str = f"({precise_c:.1f}\u00b0C)" if precise_c is not None else ""
    max_str = f"{running_max_f}\u00b0F" if running_max_f is not None else "\u2014"
    margin_str = (
        f"{margin_c:+.2f}\u00b0C ({margin_status})"
        if margin_c is not None
        else "\u2014"
    )

    conviction_html = "\n".join(conviction_rows) if conviction_rows else ""

    precise_val = f"{precise_f_str} {precise_c_str}"
    precise_row = (
        f'<tr><td style="color:#9ca3af;">Precise ({precise_source}):</td>'
        f"<td>{precise_val}</td></tr>"
    )

    td_grey = 'style="color:#9ca3af;"'
    bg_panel = "background:#16213e;border-radius:8px;padding:16px;margin:12px 0;"
    body_css = (
        "font-family:Consolas,monospace;"
        "background:#1a1a2e;color:#e0e0e0;padding:20px;"
    )
    signal_css = (
        f"background:{color};border-radius:8px;"
        "padding:20px;margin:12px 0;text-align:center;"
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="{body_css}">
<div style="max-width:600px;margin:0 auto;">

<h2 style="color:#fff;margin-bottom:4px;">SPIKE ALERT: {city} {bracket}</h2>
<p style="color:#9ca3af;margin-top:0;">Email {email_number} of {email_total} &mdash; {time_str}</p>

<div style="{bg_panel}">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">MARKET</h3>
<p style="font-size:18px;margin:0;">
{old_price}&cent; &rarr; {new_price}&cent;
(+{spike_delta}&cent;) &mdash; now at {current_price}&cent;
</p>
</div>

<div style="{bg_panel}">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">EDGE ANALYSIS</h3>
<table style="width:100%;color:#e0e0e0;font-size:14px;">
<tr><td {td_grey}>METAR (rounded):</td><td>{metar_str}</td></tr>
{precise_row}
<tr><td {td_grey}>Running max:</td><td>{max_str}</td></tr>
<tr><td {td_grey}>Margin:</td><td>{margin_str}</td></tr>
</table>
</div>

<div style="{signal_css}">
<span style="font-size:24px;font-weight:bold;color:#fff;">{label}</span>
<br>
<span style="font-size:13px;color:rgba(255,255,255,0.8);">Time risk: {time_risk}</span>
</div>

<p style="color:#d1d5db;font-size:13px;margin:8px 0;">{signal_reason}</p>

<div style="{bg_panel}">
<h3 style="color:#9ca3af;margin:0 0 8px 0;font-size:13px;">CONVICTION TREND</h3>
<table style="width:100%;color:#e0e0e0;font-size:13px;">
{conviction_html}
</table>
</div>

</div>
</body>
</html>"""


# ── Send email ───────────────────────────────────────────────────────


def send_spike_email(
    subject: str,
    html_body: str,
    gmail_address: str,
    gmail_app_password: str,
) -> None:
    """Send an HTML spike alert email to self via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["From"] = gmail_address
    msg["To"] = gmail_address  # send to self
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Sending spike alert: %s", subject)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    logger.info("Spike alert sent")

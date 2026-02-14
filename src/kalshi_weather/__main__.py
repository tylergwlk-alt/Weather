"""CLI entry point — run via `python -m kalshi_weather`.

Loads credentials from environment variables, runs the full scan,
and emails the report.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("kalshi_weather")


def main() -> int:
    """Run the full Kalshi Weather Scanner pipeline."""
    # Configure logging
    log_level = os.environ.get("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    logger.info("Kalshi Weather Scanner starting")

    # ── Load Kalshi credentials ──────────────────────────────────────
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

    if not api_key_id or not private_key_path:
        logger.error(
            "Missing Kalshi credentials. Set KALSHI_API_KEY_ID and "
            "KALSHI_PRIVATE_KEY_PATH environment variables."
        )
        return 1

    # ── Create API clients ───────────────────────────────────────────
    from kalshi_weather.kalshi_client import KalshiClient
    from kalshi_weather.weather_api import WeatherAPI

    client = KalshiClient(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
    )
    weather = WeatherAPI()

    # ── Run full scan ────────────────────────────────────────────────
    from kalshi_weather.runner import run_full_scan

    try:
        slate, output_dir = run_full_scan(client, weather)
    except Exception:
        logger.exception("Full scan failed")
        return 1
    finally:
        client.close()

    logger.info(
        "Scan complete: %d PRIMARY, %d TIGHT, %d NEAR-MISS, %d REJECTED",
        len(slate.picks_primary),
        len(slate.picks_tight),
        len(slate.picks_near_miss),
        len(slate.rejected),
    )

    # ── Send email ───────────────────────────────────────────────────
    email_to = os.environ.get("EMAIL_TO", "")
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if email_to and gmail_address and gmail_app_password:
        from kalshi_weather.emailer import send_report_email

        # Find the report and slate files in the output directory
        reports = sorted(output_dir.glob("REPORT_*.md"))
        slates = sorted(output_dir.glob("DAILY_SLATE_*.json"))

        if reports:
            report_path = reports[-1]  # Most recent
            slate_path = slates[-1] if slates else None
            subject = (
                f"Kalshi Weather — {slate.target_date_local} "
                f"({len(slate.picks_primary)} picks)"
            )
            try:
                send_report_email(
                    to_addr=email_to,
                    subject=subject,
                    report_md_path=report_path,
                    slate_json_path=slate_path,
                    gmail_address=gmail_address,
                    gmail_app_password=gmail_app_password,
                )
            except Exception:
                logger.exception("Failed to send email")
                return 1
        else:
            logger.warning("No report file found in %s — skipping email", output_dir)
    else:
        logger.info(
            "Email not configured — skipping "
            "(set EMAIL_TO, GMAIL_ADDRESS, GMAIL_APP_PASSWORD)"
        )

    logger.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

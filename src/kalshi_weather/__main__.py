"""CLI entry point — run via `python -m kalshi_weather`.

Subcommands:
  scan  — Full Kalshi Weather Scanner pipeline (default)
  edge  — Temperature edge analysis from NWS sources
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

logger = logging.getLogger("kalshi_weather")


# ── Scan subcommand (original behavior) ──────────────────────────────


def _run_scan() -> int:
    """Run the full Kalshi Weather Scanner pipeline."""
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


# ── Edge subcommand ──────────────────────────────────────────────────


def _output(text: str) -> None:
    """Write text to stdout (avoids bare print() for lint compliance)."""
    sys.stdout.write(text + "\n")


def _run_edge(city: str | None, watch: bool, interval: int) -> int:
    """Run temperature edge analysis."""
    from kalshi_weather.edge import (
        analyze_all_cities,
        analyze_city,
        format_edge_report,
        format_edge_summary,
    )
    from kalshi_weather.nws_scraper import NWSScraper

    with NWSScraper() as scraper:
        while True:
            if city:
                report = analyze_city(city, scraper)
                if report is None:
                    logger.error("Could not analyze city: %s", city)
                    return 1
                _output(format_edge_report(report))
            else:
                reports = analyze_all_cities(scraper)
                if not reports:
                    logger.error("No cities could be analyzed")
                    return 1
                _output(format_edge_summary(reports))

                # Detailed reports for cities with actionable signals
                for r in reports:
                    if r.signal.value in ("STRONG_BUY", "BUY", "CAUTION"):
                        _output("")
                        _output(format_edge_report(r))

            if not watch:
                break

            logger.info("Watching — next update in %d seconds", interval)
            time.sleep(interval)
            _output("\n" + "=" * 60 + "\n")

    return 0


# ── Main with argparse ───────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate subcommand.

    Parameters
    ----------
    argv : list of CLI args. Defaults to [] (runs 'scan').
           Pass sys.argv[1:] for real CLI usage.
    """
    if argv is None:
        argv = []
    # Configure logging early
    log_level = os.environ.get("LOGLEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(
        prog="kalshi_weather",
        description="Kalshi Weather Scanner & Temperature Edge Bot",
    )
    subparsers = parser.add_subparsers(dest="command")

    # scan subcommand (default)
    subparsers.add_parser("scan", help="Run full Kalshi Weather Scanner pipeline")

    # edge subcommand
    edge_parser = subparsers.add_parser(
        "edge", help="Temperature edge analysis from NWS sources"
    )
    edge_parser.add_argument(
        "--city", type=str, default=None,
        help="Analyze a single city (e.g. 'Chicago'). Omit for all 26 cities.",
    )
    edge_parser.add_argument(
        "--watch", action="store_true",
        help="Continuously poll at --interval seconds",
    )
    edge_parser.add_argument(
        "--interval", type=int, default=300,
        help="Polling interval in seconds (default: 300)",
    )

    args = parser.parse_args(argv)

    # Default to 'scan' if no subcommand given
    if args.command is None or args.command == "scan":
        return _run_scan()
    elif args.command == "edge":
        return _run_edge(args.city, args.watch, args.interval)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

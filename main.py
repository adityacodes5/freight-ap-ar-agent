#!/usr/bin/env python3
"""
Demo Logistics AI Agent — entry point

Usage:
  python main.py                        # run Phase 1 invoice processing
  python main.py --dry-run              # preview without writing to the sheet
  python main.py --query "..."          # override email search query
  python main.py --get-account-id       # print Zoho account ID (one-time setup)
  python main.py --list-processed       # show all invoices already processed
  python main.py -v                     # verbose / debug logging
"""

import sys
import argparse
from loguru import logger
from config import Config
from agent import DemoAgent
import state as invoice_state


def setup_logging(verbose: bool = False) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )
    logger.add(
        "logistics_agent.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )


def cmd_get_account_id(config: Config) -> None:
    from tools.zoho_mail import ZohoMailClient
    zoho = ZohoMailClient(config)
    accounts = zoho.get_accounts()
    if not accounts:
        print("No accounts found — check ZOHO_CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN")
        return
    print("\nYour Zoho Mail accounts:")
    print("-" * 50)
    for acc in accounts:
        print(f"  accountId : {acc.get('accountId')}")
        print(f"  email     : {acc.get('emailAddress')}")
        print()
    print("Add the correct accountId to your .env as ZOHO_ACCOUNT_ID")


def cmd_list_processed() -> None:
    entries = invoice_state.list_processed()
    if not entries:
        print("No invoices recorded as processed yet.")
        return
    print(f"\n{'Processed invoices':=<60}")
    for e in entries:
        print(
            f"  {e['processed_at'][:19]}  "
            f"Invoice #{e.get('invoice_number') or '?':15}  "
            f"{e.get('carrier_name') or '?'}"
        )
    print(f"\nTotal: {len(entries)}")


def cmd_run_phase1(
    config: Config,
    query: str | None = None,
    dry_run: bool = False,
) -> None:
    agent = DemoAgent(config, dry_run=dry_run)
    summary = agent.run_phase1()
    print("\n" + "=" * 60)
    print("AGENT SUMMARY")
    print("=" * 60)
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo Logistics Phase 1 AI Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be updated without writing to the sheet",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Override the default Zoho email search query",
    )
    parser.add_argument(
        "--get-account-id",
        action="store_true",
        help="Print Zoho Mail account IDs (run once during setup)",
    )
    parser.add_argument(
        "--list-processed",
        action="store_true",
        help="Show all invoices that have already been processed",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if args.list_processed:
        cmd_list_processed()
        return

    try:
        config = Config()
    except ValueError as exc:
        logger.error("Configuration error: {}", exc)
        logger.info("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    if args.get_account_id:
        cmd_get_account_id(config)
    else:
        cmd_run_phase1(config, query=args.query, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

"""Command-line entry point for the internship outreach system.

Usage:
    python main.py                    -> show status
    python main.py init-excel         -> create the workbook
    python main.py load-samples       -> add 10 test companies
    python main.py show-companies     -> list companies in the workbook
    python main.py check-excel        -> validate the workbook structure

Later stages add: qualify, score, contacts, draft, send, replies, followups
"""

import argparse
import logging
import sys

from app import __version__, schema
from app.config import Config, ConfigError, load_config
from app.excel import (
    ExcelError,
    append_rows,
    create_workbook,
    read_companies,
    read_settings,
    validate_workbook,
)
from app.logging_setup import setup_logging
from app.sample_data import build_company_records

logger = logging.getLogger("main")


def cmd_status(config: Config, args) -> int:
    """Print a startup summary so you can confirm configuration is loading."""
    print()
    print("=" * 58)
    print(f"  Internship Outreach Agent  v{__version__}")
    print("=" * 58)
    print()
    print("  Configuration")
    print(f"    Excel workbook   : {config.excel_path}")
    workbook_exists = config.excel_path.exists()
    print(f"    Workbook exists  : {'yes' if workbook_exists else 'no (run: python main.py init-excel)'}")
    print(f"    Log level        : {config.log_level}")
    print()
    print("  AI")
    # Never print the key itself -- only whether one is configured.
    print(f"    OpenAI key       : {'configured' if config.has_openai_key else 'NOT SET (needed from Stage 3)'}")
    print(f"    Model            : {config.openai_model}")
    print()
    print("  Email safety")
    print(f"    Dry run          : {config.dry_run}  (no email is sent while true)")
    print(f"    Daily send limit : {config.daily_send_limit}")
    print(f"    Batch size       : {config.batch_size}")
    print(f"    Delay between    : {config.seconds_between_sends}s")

    # If the workbook exists, show a quick count of what is in it.
    if workbook_exists:
        try:
            companies = read_companies(config.excel_path)
            by_status: dict[str, int] = {}
            for company in companies:
                status = str(company.get("Research Status", "")).strip() or "(blank)"
                by_status[status] = by_status.get(status, 0) + 1

            print()
            print("  Workbook contents")
            print(f"    Companies        : {len(companies)}")
            for status, count in sorted(by_status.items()):
                print(f"      {status:<16}: {count}")
        except ExcelError as exc:
            print()
            print(f"  Could not read workbook: {exc}")

    print()
    logger.info("Status check completed successfully.")
    return 0


def cmd_init_excel(config: Config, args) -> int:
    """Create the workbook with its four sheets."""
    try:
        path = create_workbook(config.excel_path, overwrite=args.overwrite)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\nCreated workbook: {path}")
    print(f"Sheets: {', '.join(schema.SHEET_NAMES)}")
    print("\nNext: open the Settings sheet and replace every FILL_IN value")
    print("      (your email, LinkedIn, GitHub, availability, locations).")
    print("Then: python main.py load-samples\n")
    return 0


def cmd_load_samples(config: Config, args) -> int:
    """Load the 10 test companies, skipping any already present."""
    try:
        existing = read_companies(config.excel_path)
        existing_names = {
            str(c.get("Company Name", "")).strip().lower() for c in existing
        }

        # Duplicate prevention, in miniature. The same principle applies
        # later when we create outreach records.
        records = [
            record
            for record in build_company_records(start_index=len(existing) + 1)
            if record["Company Name"].strip().lower() not in existing_names
        ]

        skipped = 10 - len(records)
        if skipped:
            logger.info("Skipping %d company/companies already in the workbook.", skipped)

        if not records:
            print("\nAll sample companies are already loaded. Nothing to do.\n")
            return 0

        append_rows(config.excel_path, schema.COMPANIES, records)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\nAdded {len(records)} companies to the Companies sheet.")
    print("All have Research Status = PENDING -- nothing has been researched yet.")
    print("\nNext: python main.py show-companies\n")
    return 0


def cmd_show_companies(config: Config, args) -> int:
    """Print the companies as a readable table."""
    try:
        companies = read_companies(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not companies:
        print("\nNo companies yet. Run: python main.py load-samples\n")
        return 0

    print()
    print(f"{'ID':<6} {'Company Name':<42} {'Status':<14} {'Priority':<8}")
    print("-" * 74)
    for company in companies:
        print(
            f"{str(company.get('Company ID', '')):<6} "
            f"{str(company.get('Company Name', ''))[:41]:<42} "
            f"{str(company.get('Research Status', '')):<14} "
            f"{str(company.get('Priority', '')):<8}"
        )
    print("-" * 74)
    print(f"{len(companies)} companies\n")
    return 0


def cmd_check_excel(config: Config, args) -> int:
    """Validate the workbook structure and report any problems."""
    problems = validate_workbook(config.excel_path)

    if problems:
        print("\nProblems found in the workbook:\n")
        for problem in problems:
            print(f"  - {problem}")
        print()
        return 1

    print(f"\nWorkbook structure is valid: {config.excel_path}")

    # Also warn about unfilled profile placeholders -- these matter from
    # Stage 6, when emails start quoting your details.
    try:
        settings = read_settings(config.excel_path)
        unfilled = sorted(k for k, v in settings.items() if v == "FILL_IN")
        if unfilled:
            print("\nSettings still to fill in (needed by Stage 6):")
            for key in unfilled:
                print(f"  - {key}")
    except ExcelError as exc:
        logger.warning("Could not read Settings: %s", exc)

    print()
    return 0


COMMANDS = {
    "status": cmd_status,
    "init-excel": cmd_init_excel,
    "load-samples": cmd_load_samples,
    "show-companies": cmd_show_companies,
    "check-excel": cmd_check_excel,
}


def build_parser() -> argparse.ArgumentParser:
    """Define the CLI. One subcommand per stage, added as we build them."""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="AI-assisted internship & industrial attachment outreach system.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override LOG_LEVEL from .env (DEBUG, INFO, WARNING, ERROR).",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show configuration and readiness.")

    init_parser = subparsers.add_parser(
        "init-excel", help="Create the Excel workbook."
    )
    init_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing workbook. WARNING: deletes all data in it.",
    )

    subparsers.add_parser("load-samples", help="Add the 10 test companies.")
    subparsers.add_parser("show-companies", help="List companies in the workbook.")
    subparsers.add_parser("check-excel", help="Validate the workbook structure.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, dispatch to the chosen command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Bootstrap logging at INFO so config-loading warnings are visible,
    # then re-configure once we know the configured level.
    setup_logging("INFO")

    try:
        config = load_config()
    except ConfigError as exc:
        # A bad .env value should produce one clear line, not a traceback.
        logger.error("Configuration error: %s", exc)
        return 1

    setup_logging(args.log_level or config.log_level)

    command = args.command or "status"
    handler = COMMANDS.get(command)
    if handler is None:
        logger.error("Unknown command: %s", command)
        return 1

    return handler(config, args)


if __name__ == "__main__":
    sys.exit(main())

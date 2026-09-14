"""Command-line entry point for the internship outreach system.

Usage:
    python main.py                    -> show status
    python main.py init-excel         -> create the workbook
    python main.py load-samples       -> add 10 test companies
    python main.py show-companies     -> list companies in the workbook
    python main.py check-excel        -> validate the workbook structure
    python main.py qualify            -> research + AI-qualify companies
    python main.py show-results       -> qualification results table

Later stages add: contacts, draft, send, replies, followups
"""

import argparse
import logging
import sys
import time

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
from app.excel import read_settings as _read_settings  # noqa: F401 (clarity)
from app.logging_setup import setup_logging
from app.ai import AIError, get_ai_caller
from app.qualification import (
    QualificationError,
    qualify_company,
    to_company_updates,
)
from app.research import research_company
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
    print(f"    Provider         : {config.ai_provider}")
    print(f"    API key          : {'configured' if config.has_ai_key else 'NOT SET (run with --mock, or add AI_API_KEY)'}")
    print(f"    Model            : {config.ai_model}")
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


def cmd_fix_urls(config: Config, args) -> int:
    """Update Website values in the workbook from the sample-data list.

    Needed when a company's URL is corrected after its row was already
    created. Only the Website column is touched -- everything else you
    have edited by hand is left alone.
    """
    from app.excel import update_rows
    from app.sample_data import SAMPLE_COMPANIES

    correct = {name.strip().lower(): website for name, website, *_ in SAMPLE_COMPANIES}

    try:
        companies = read_companies(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    updates: dict[int, dict] = {}
    for company in companies:
        key = str(company.get("Company Name", "")).strip().lower()
        wanted = correct.get(key)
        if wanted and str(company.get("Website", "")).strip() != wanted:
            print(f"  {company['Company Name']}: {company.get('Website')} -> {wanted}")
            updates[company["_row"]] = {
                "Website": wanted,
                # The old result was based on the wrong URL, so it must be
                # re-researched rather than left looking authoritative.
                "Research Status": schema.RESEARCH_PENDING,
            }

    if not updates:
        print("\nAll websites already match the sample list.\n")
        return 0

    try:
        update_rows(config.excel_path, schema.COMPANIES, updates)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\nUpdated {len(updates)} website(s) and reset them to PENDING.")
    print("Next: python main.py qualify\n")
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


def cmd_qualify(config: Config, args) -> int:
    """Research and AI-qualify companies, then write results to Excel.

    Only companies with Research Status = PENDING are processed, so
    re-running the command never repeats work you have already paid for.
    Use --force to re-qualify everything.
    """
    from app.excel import read_settings, update_rows

    # Choose the AI. The mock costs nothing and needs no network, so it is
    # the safe default while developing.
    if args.mock:
        print("\nMOCK MODE -- no real AI calls, results are meaningless.\n")
    elif not config.has_ai_key:
        logger.error(
            "No AI API key configured. Add AI_API_KEY to .env, or run with "
            "--mock to test the pipeline without one."
        )
        return 1

    try:
        ai_caller = get_ai_caller(config, use_mock=args.mock)
    except AIError as exc:
        logger.error("%s", exc)
        return 1

    if not args.mock:
        print(f"\nProvider: {config.ai_provider} ({config.ai_model})\n")

    try:
        companies = read_companies(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    # Pick the work queue.
    if args.force:
        queue = companies
    else:
        queue = [
            c for c in companies
            if str(c.get("Research Status", "")).strip().upper()
            in ("", schema.RESEARCH_PENDING, schema.RESEARCH_ERROR)
        ]

    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print("\nNothing to qualify. All companies already have a status.")
        print("Use --force to re-qualify them.\n")
        return 0

    print(f"Qualifying {len(queue)} company/companies...\n")

    updates: dict[int, dict] = {}
    counts: dict[str, int] = {}
    failures: list[str] = []

    for index, company in enumerate(queue):
        name = str(company.get("Company Name", "?"))

        # Pace the calls. Free API tiers cap requests per minute, and a
        # short pause is far cheaper than hitting the limit and retrying.
        if index > 0 and not args.mock and config.seconds_between_ai_calls:
            time.sleep(config.seconds_between_ai_calls)

        # Step 1: gather real evidence (no AI involved).
        pack = research_company(name, str(company.get("Website", "")))

        # Step 2: ask the AI to judge only that evidence.
        try:
            result = qualify_company(
                config, company, pack, settings, ai_caller=ai_caller
            )
        except (QualificationError, AIError) as exc:
            # A failure must be visible in the workbook, never silent.
            logger.error("%s", exc)
            failures.append(name)
            # Clear any stale scores from a previous run, so an ERROR row
            # never shows an old score that no longer reflects a real result.
            updates[company["_row"]] = {
                "Research Status": schema.RESEARCH_ERROR,
                "Qualification Reason": f"Qualification failed: {exc}"[:500],
                "Total Score": "",
                "Priority": "",
            }
            continue

        row_updates = to_company_updates(result)
        # Prefer the careers URL we actually fetched over one the AI names.
        if pack.careers_url:
            row_updates["Careers URL"] = pack.careers_url
        updates[company["_row"]] = row_updates

        counts[result.classification] = counts.get(result.classification, 0) + 1
        print(
            f"  {result.classification:<13} {result.total_score:>3}/100  "
            f"{result.priority:<6}  {name[:40]}"
        )

    # Step 3: one batched write, rather than a save per company.
    try:
        update_rows(config.excel_path, schema.COMPANIES, updates)
    except ExcelError as exc:
        logger.error("Results could NOT be saved: %s", exc)
        return 1

    print()
    for status, count in sorted(counts.items()):
        print(f"  {status:<14}: {count}")
    if failures:
        print(f"  {'ERROR':<14}: {len(failures)}  ({', '.join(failures)})")
    print(f"\nSaved to {config.excel_path}")
    print("Next: python main.py show-results\n")
    return 0


def cmd_show_results(config: Config, args) -> int:
    """Show qualification results, highest score first."""
    try:
        companies = read_companies(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    researched = [
        c for c in companies
        if str(c.get("Research Status", "")).strip().upper()
        not in ("", schema.RESEARCH_PENDING)
    ]
    if not researched:
        print("\nNothing qualified yet. Run: python main.py qualify --mock\n")
        return 0

    def sort_key(company):
        score = company.get("Total Score", 0)
        return -(score if isinstance(score, (int, float)) else 0)

    print()
    print(f"{'ID':<6} {'Company':<34} {'Status':<13} {'Score':>5} {'Pri':<4} Reason")
    print("-" * 110)
    for company in sorted(researched, key=sort_key):
        reason = str(company.get("Qualification Reason", ""))
        print(
            f"{str(company.get('Company ID','')):<6} "
            f"{str(company.get('Company Name',''))[:33]:<34} "
            f"{str(company.get('Research Status','')):<13} "
            f"{str(company.get('Total Score','')):>5} "
            f"{str(company.get('Priority','')):<4} "
            f"{reason[:44]}"
        )
    print("-" * 110)
    print(f"{len(researched)} researched\n")
    return 0


COMMANDS = {
    "status": cmd_status,
    "init-excel": cmd_init_excel,
    "load-samples": cmd_load_samples,
    "show-companies": cmd_show_companies,
    "check-excel": cmd_check_excel,
    "fix-urls": cmd_fix_urls,
    "qualify": cmd_qualify,
    "show-results": cmd_show_results,
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

    qualify_parser = subparsers.add_parser(
        "qualify", help="Research and AI-qualify companies."
    )
    qualify_parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the fake AI: free, offline, meaningless results. For testing plumbing.",
    )
    qualify_parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N companies (useful for a cheap first real run).",
    )
    qualify_parser.add_argument(
        "--force", action="store_true",
        help="Re-qualify companies that already have a status.",
    )

    subparsers.add_parser(
        "fix-urls", help="Sync corrected websites from sample_data into the workbook."
    )
    subparsers.add_parser("show-results", help="Show qualification results.")

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

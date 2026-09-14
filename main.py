"""Command-line entry point for the internship outreach system.

Usage:
    python main.py           -> show status (default)
    python main.py status    -> same thing, explicitly

Later stages add subcommands:
    init-excel, qualify, score, contacts, draft, send, replies, followups
"""

import argparse
import logging
import sys

from app import __version__
from app.config import ConfigError, load_config
from app.logging_setup import setup_logging

logger = logging.getLogger("main")


def cmd_status(config) -> int:
    """Print a startup summary so you can confirm configuration is loading.

    Returns a process exit code: 0 = success.
    """
    print()
    print("=" * 58)
    print(f"  Internship Outreach Agent  v{__version__}")
    print("=" * 58)
    print()
    print("  Configuration")
    print(f"    Excel workbook   : {config.excel_path}")
    print(f"    Workbook exists  : {'yes' if config.excel_path.exists() else 'no (Stage 2 creates it)'}")
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
    print()
    print("  Stage 1 complete -- configuration and logging are working.")
    print()

    logger.info("Status check completed successfully.")
    return 0


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

    # No subcommand given -> default to status.
    command = args.command or "status"

    if command == "status":
        return cmd_status(config)

    logger.error("Unknown command: %s", command)
    return 1


if __name__ == "__main__":
    sys.exit(main())

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
from pathlib import Path

from app import __version__, schema
from app.config import PROJECT_ROOT, Config, ConfigError, load_config
from app.excel import (
    ExcelError,
    append_rows,
    create_workbook,
    read_companies,
    read_contacts,
    read_outreach,
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


# Ready-made searches for the kinds of organisation worth approaching.
# Used by `discover --preset`.
SEARCH_PRESETS = {
    # --- Technology companies -------------------------------------------
    "data": [
        "data analytics consultancy Nairobi",
        "business intelligence company Kenya",
        "data engineering company Nairobi",
    ],
    "software": [
        "software development company Nairobi Kenya",
        "custom software company Kenya enterprise",
        "web application development company Nairobi",
    ],
    "startups": [
        "Nairobi startup engineering team careers",
        "Kenyan startup hiring software engineers",
        "Y Combinator startup Kenya Nairobi",
    ],
    "fintech": [
        "fintech company Nairobi Kenya",
        "payments company Kenya technology",
        "digital lending company Kenya",
    ],

    # --- Development sector ---------------------------------------------
    "ngo": [
        "NGO Nairobi monitoring and evaluation data",
        "international NGO Kenya data analyst",
        "humanitarian organisation Kenya data team",
    ],
    "international": [
        "UN agency Kenya Nairobi data",
        "World Bank Kenya office",
        "international organisation Nairobi data systems",
    ],
    "research": [
        "research institute Kenya data science",
        "policy research organisation Nairobi",
        "think tank Kenya data analysis",
    ],
    "mande": [
        "monitoring and evaluation consultancy Nairobi",
        "survey data collection company Kenya",
        "impact evaluation firm Kenya",
    ],

    # --- Public sector ---------------------------------------------------
    "government": [
        "Kenya government agency ICT department",
        "county government Kenya data systems",
        "Kenya state corporation technology department",
    ],

    # --- Established employers -------------------------------------------
    "banking": [
        "bank Kenya technology department careers",
        "insurance company Kenya data analytics",
        "SACCO management software Kenya",
    ],
    "telecom": [
        "telecommunications company Kenya careers",
        "internet service provider Kenya technology",
    ],
    "consulting": [
        "technology consulting firm Nairobi",
        "management consultancy Kenya data analytics",
    ],

    # --- Sector technology -----------------------------------------------
    "health": [
        "health technology company Kenya",
        "digital health company Nairobi",
        "health data company Kenya",
    ],
    "education": [
        "edtech company Kenya Nairobi",
        "education data company Kenya",
    ],
    "agritech": [
        "agritech company Kenya data",
        "agricultural technology company Nairobi",
    ],
    "logistics": [
        "logistics technology company Nairobi",
        "supply chain software Kenya",
    ],
    "energy": [
        "renewable energy company Kenya data",
        "solar company Kenya technology",
    ],
}

# Presets grouped for the help text, so it is obvious what is on offer.
PRESET_GROUPS = {
    "Technology": ["data", "software", "startups", "fintech"],
    "Development sector": ["ngo", "international", "research", "mande"],
    "Public sector": ["government"],
    "Established employers": ["banking", "telecom", "consulting"],
    "Sector technology": ["health", "education", "agritech", "logistics", "energy"],
}


def cmd_discover(config: Config, args) -> int:
    """Find new organisations through a real web search.

    Results come from a search index, not from the AI's memory. Every
    candidate's website is checked before it is offered, so the workbook
    does not fill up with domains that cannot be researched.
    """
    from app.discover import (
        DUCKDUCKGO_DELAY,
        DiscoverError,
        existing_domains,
        search,
        to_candidates,
        verify_reachable,
    )
    from app.excel import read_settings

    # Work out what to search for.
    if getattr(args, "all", False):
        queries = [q for name in SEARCH_PRESETS for q in SEARCH_PRESETS[name]]
        print(f"\n  Running every preset: {len(queries)} searches. This will be slow,")
        print("  and free search engines may rate-limit part way through.")
        print("  Whatever succeeds is kept in the staging file.\n")
    elif args.preset:
        queries = SEARCH_PRESETS.get(args.preset)
        if not queries:
            logger.error(
                "Unknown preset %r. Available: %s",
                args.preset, ", ".join(sorted(SEARCH_PRESETS)),
            )
            return 1
    elif args.query:
        queries = [" ".join(args.query)]
    else:
        print("\n  Give a search, or choose a preset:\n")
        print('      python main.py discover "monitoring and evaluation consultancy Nairobi"')
        print("      python main.py discover --preset ngo\n")
        for group, names in PRESET_GROUPS.items():
            print(f"    {group}")
            for name in names:
                first = SEARCH_PRESETS[name][0]
                print(f'      --preset {name:<14} e.g. "{first}"')
            print()
        print("    Or --all to run every preset in turn "
              "(slow; may hit rate limits).\n")
        return 1

    try:
        companies = read_companies(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    known = existing_domains(companies)

    # 1. Search. Real results, from a real index.
    all_candidates = []
    seen = set()
    for query in queries:
        print(f"\n  Searching: {query}")
        try:
            results = search(config, query, count=args.count, engine=args.engine)
        except DiscoverError as exc:
            logger.error("%s", exc)
            return 1

        for candidate in to_candidates(results, query):
            if candidate.domain in known:
                logger.debug("%s already in the workbook.", candidate.domain)
                continue
            if candidate.domain in seen:
                continue
            seen.add(candidate.domain)
            all_candidates.append(candidate)

        # Pace the queries. DuckDuckGo has no published limit but will
        # block a burst, and Brave's free credit allows about one a second.
        if len(queries) > 1:
            time.sleep(DUCKDUCKGO_DELAY if args.engine == "duckduckgo" else 1.2)

    if not all_candidates:
        print("\n  No new organisations found. They may all be in the workbook "
              "already.\n")
        return 0

    print(f"\n  {len(all_candidates)} new candidate(s) found.")

    # 2. Screen with the AI -- it judges the real results, and cannot add
    #    anything of its own.
    if args.no_screen:
        kept = all_candidates
    else:
        if not config.has_ai_key:
            logger.warning("No AI key set -- skipping screening.")
            kept = all_candidates
        else:
            kept = _screen_candidates(config, all_candidates, settings)
            print(f"  {len(kept)} kept after screening.")

    if not kept:
        print("\n  Nothing worth adding.\n")
        return 0

    # 3. Verify each site actually loads. An unreachable domain can never
    #    be researched, so adding it only creates a dead row.
    print(f"\n  Checking {len(kept)} website(s) load...\n")
    reachable = []
    for candidate in kept:
        ok, note = verify_reachable(candidate)
        mark = "ok  " if ok else "DEAD"
        print(f"    {mark}  {candidate.name[:34]:<36} {candidate.url[:38]}")
        if ok:
            reachable.append(candidate)

    if not reachable:
        print("\n  None of the websites could be reached.\n")
        return 0

    # 4. Write a CSV for you to review before importing.
    #
    # APPEND rather than overwrite. Running discover twice in a row used
    # to destroy the first run's results, which is a nasty surprise when
    # you are searching several categories before importing any of them.
    import csv

    output = Path(args.output) if args.output else config.excel_path.parent / "discovered.csv"

    HEADERS = ["Company Name", "Website", "Country", "Region", "Industry", "Source"]

    # Read what is already staged, so we neither lose it nor duplicate it.
    staged: list[dict] = []
    staged_domains: set[str] = set()
    if output.exists() and not args.replace:
        try:
            with output.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    staged.append(row)
                    site = str(row.get("Website", "")).strip()
                    if site:
                        from urllib.parse import urlparse

                        host = urlparse(site).netloc.lower().removeprefix("www.")
                        if host:
                            staged_domains.add(host)
        except (OSError, csv.Error) as exc:
            logger.warning("Could not read existing %s (%s) -- starting fresh.",
                           output.name, exc)

    fresh = [c for c in reachable if c.domain not in staged_domains]
    skipped_staged = len(reachable) - len(fresh)
    if skipped_staged:
        print(f"\n  {skipped_staged} already waiting in {output.name} -- not added twice.")

    try:
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADERS)
            for row in staged:
                writer.writerow([row.get(h, "") for h in HEADERS])
            for candidate in fresh:
                writer.writerow([
                    candidate.name, candidate.url, args.country, args.region,
                    getattr(candidate, "sector", ""), f"Search: {candidate.query}",
                ])
    except OSError as exc:
        logger.error("Could not write %s: %s", output, exc)
        return 1

    total_staged = len(staged) + len(fresh)
    if fresh:
        print(f"\n  Added {len(fresh)} new organisation(s) to {output.name}.")
    print(f"  {total_staged} organisation(s) now waiting to be imported.")
    print("\n  Search more categories if you like -- results accumulate in that file.")
    print("  When you are ready:")
    print("      1. Open it and delete any you do not want")
    print(f"      2. python main.py import-companies {output}")
    print("      3. python main.py qualify\n")
    print("  Importing clears the file, so nothing is offered twice.\n")
    return 0


def _screen_candidates(config: Config, candidates: list, settings: dict) -> list:
    """Ask the AI which candidates are worth researching.

    It sees only the real search results and may keep or drop them. It
    cannot add an organisation, because we match its answers back to the
    original list by index.
    """
    from app.email_drafts import format_student_profile
    from app.qualification import extract_json

    prompt_path = PROJECT_ROOT / "prompts" / "discover.txt"
    if not prompt_path.exists():
        logger.warning("Screening prompt missing -- keeping all candidates.")
        return candidates

    listing = "\n\n".join(
        f"{index}. {c.name}\n   URL: {c.url}\n   {c.description[:220]}"
        for index, c in enumerate(candidates)
    )

    prompt = prompt_path.read_text(encoding="utf-8").format(
        student_profile=format_student_profile(settings),
        country=settings.get("Preferred Locations") or "Kenya",
        results=listing,
    )

    try:
        raw = get_ai_caller(config)(config, prompt)
        data = extract_json(raw)
    except Exception as exc:
        logger.warning("Screening failed (%s) -- keeping all candidates.", exc)
        return candidates

    kept = []
    for entry in data.get("keep", []):
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(candidates):
            # The model referred to something that is not in the list.
            logger.warning("Screening returned index %s, which does not exist.", index)
            continue

        candidate = candidates[index]
        # Take the tidied name only if it plausibly refers to the same
        # organisation; otherwise keep what the real page said.
        suggested = str(entry.get("name", "")).strip()
        if suggested:
            candidate.name = suggested[:80]
        candidate.sector = str(entry.get("sector", "")).strip()
        kept.append(candidate)

    return kept


def cmd_import_companies(config: Config, args) -> int:
    """Add companies to the workbook from a CSV file.

    The CSV needs at minimum a "Company Name" column. Any other column
    matching a Companies-sheet heading is imported too; unknown columns
    are ignored with a warning rather than silently dropped.

    Companies already in the workbook are skipped by name, so you can
    re-run this safely after editing the file.
    """
    import csv

    from app.excel import next_id, today, today

    path = Path(args.csv_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if not path.exists():
        # The commonest cause is not a mistake at all: the file was
        # cleared because it was already imported. Say so, rather than
        # implying the user got the format wrong.
        archive = path.parent / "discovered-imported.csv"
        if path.name == "discovered.csv" and archive.exists():
            print(f"\n  {path.name} is empty -- you have already imported it.")
            print(f"  Its contents were archived to {archive.name}.\n")
            print("  Nothing to do here. Your next step is probably:")
            print("      python main.py qualify        (research the new companies)")
            print("\n  To find more companies first:")
            print('      python main.py discover "your search here"\n')
            return 0

        logger.error(
            "CSV not found: %s\n"
            "         If you meant to find new companies first, run:\n"
            '             python main.py discover "your search here"\n'
            "         Or point this at your own CSV file, which needs a header "
            "row with\n"
            "         at least a 'Company Name' column.",
            path,
        )
        return 1

    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        logger.error("Could not read %s: %s", path, exc)
        return 1

    if not rows:
        print(f"\n  {path} has no data rows.\n")
        return 0

    if "Company Name" not in rows[0]:
        logger.error(
            "The CSV has no 'Company Name' column. Found: %s",
            ", ".join(rows[0].keys()),
        )
        return 1

    unknown = set(rows[0].keys()) - set(schema.COMPANY_COLUMNS)
    if unknown:
        logger.warning(
            "Ignoring column(s) that are not on the Companies sheet: %s",
            ", ".join(sorted(unknown)),
        )

    try:
        existing = read_companies(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    existing_names = {
        str(c.get("Company Name", "")).strip().lower() for c in existing
    }

    next_number = int(next_id(existing, "Company ID", "C")[1:])
    new_rows = []
    skipped = []

    for row in rows:
        name = str(row.get("Company Name", "")).strip()
        if not name:
            continue
        if name.lower() in existing_names:
            skipped.append(name)
            continue

        record = {
            column: str(row.get(column, "")).strip()
            for column in schema.COMPANY_COLUMNS
            if row.get(column)
        }
        record["Company ID"] = f"C{next_number:03d}"
        record["Company Name"] = name
        # Nothing has been researched yet, and the workbook must not
        # imply otherwise.
        record["Research Status"] = schema.RESEARCH_PENDING
        record.setdefault("Source", f"Imported from {path.name}")
        record["Date Added"] = today()

        new_rows.append(record)
        existing_names.add(name.lower())
        next_number += 1

    if skipped:
        print(f"\n  Skipped {len(skipped)} company/companies already in the workbook.")

    if not new_rows:
        print("\n  Nothing new to import.\n")
        return 0

    try:
        append_rows(config.excel_path, schema.COMPANIES, new_rows)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  Imported {len(new_rows)} company/companies, all PENDING.")
    print(f"  Total in workbook: {len(existing) + len(new_rows)}")

    # Clear the discovery staging file once its contents are safely in the
    # workbook. Without this you would have to remember to empty it by
    # hand, and a stale file makes it unclear what is still waiting.
    # The file is moved rather than deleted, so a mistaken import is
    # recoverable.
    default_staging = config.excel_path.parent / "discovered.csv"
    if path.resolve() == default_staging.resolve() and not args.keep:
        archive = path.parent / "discovered-imported.csv"
        try:
            path.replace(archive)
            print(f"\n  Cleared {path.name} (previous contents kept at {archive.name}).")
        except OSError as exc:
            logger.warning("Could not clear %s: %s", path.name, exc)

    print("\n  Next: python main.py qualify\n")
    return 0


def cmd_export_csv(config: Config, args) -> int:
    """Write each sheet out as a plain CSV file.

    Useful when Excel is being awkward, or when you just want to open the
    data in something simpler. CSVs open in Excel, Numbers, Google Sheets
    or a text editor, and they cannot show you a stale cached copy.
    """
    import csv

    from app.excel import read_sheet

    out_dir = Path(args.output) if args.output else config.excel_path.parent / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for sheet_name in schema.SHEET_NAMES:
        try:
            rows = read_sheet(config.excel_path, sheet_name)
        except ExcelError as exc:
            logger.error("%s", exc)
            return 1

        columns = schema.SHEET_COLUMNS[sheet_name]
        path = out_dir / f"{sheet_name.lower()}.csv"
        try:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in rows:
                    writer.writerow([row.get(c, "") for c in columns])
        except OSError as exc:
            logger.error("Could not write %s: %s", path, exc)
            return 1

        written.append((path, len(rows)))

    print()
    for path, count in written:
        print(f"  {count:>4} rows -> {path}")
    print(f"\n  Open any of these in Excel, Numbers or a text editor.\n")
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

    # Back up before a bulk change: the workbook is the only copy of
    # your research, and a long run that goes wrong should be recoverable.
    from app.excel import backup

    saved = backup(config.excel_path, "qualify")
    if saved:
        logger.info("Backed up the workbook to %s", saved.name)

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


def cmd_find_contacts(config: Config, args) -> int:
    """Find real contact routes for qualified companies.

    Only QUALIFY companies are processed by default -- there is no point
    finding contacts for a company you will not email. Use --include-review
    to cover NEEDS_REVIEW as well.
    """
    from app.contacts import (
        build_contact_rows,
        classify_contacts,
        existing_contact_keys,
        find_contacts,
    )
    from app.excel import next_id, today

    if args.mock:
        print("\nMOCK MODE -- contact classification is faked.\n")
    elif not config.has_ai_key:
        logger.error(
            "No AI API key configured. Add AI_API_KEY to .env, or use --mock."
        )
        return 1

    try:
        ai_caller = get_ai_caller(config, use_mock=args.mock)
    except AIError as exc:
        logger.error("%s", exc)
        return 1

    try:
        companies = read_companies(config.excel_path)
        existing = read_contacts(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    wanted = {schema.RESEARCH_QUALIFY}
    if args.include_review:
        wanted.add(schema.RESEARCH_NEEDS_REVIEW)

    queue = [
        c for c in companies
        if str(c.get("Research Status", "")).strip().upper() in wanted
    ]
    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print(
            f"\nNo companies with status {' or '.join(sorted(wanted))}. "
            "Run `python main.py qualify` first,\nor add --include-review.\n"
        )
        return 0

    # Skip companies already searched. Re-fetching their websites on every
    # run is slow and finds nothing new.
    #
    # Two things count as "already searched":
    #   1. A contact row exists for them.
    #   2. The search ran and found nothing, recorded as a NONE_FOUND row.
    # Without the second, a company with no published contact details would
    # be retried on every run forever.
    searched_ids = {str(c.get("Company ID", "")).strip() for c in existing}

    if not args.recheck:
        before = len(queue)
        queue = [c for c in queue
                 if str(c.get("Company ID", "")).strip() not in searched_ids]
        skipped_already = before - len(queue)
        if skipped_already:
            print(f"\n  Skipping {skipped_already} company/companies already "
                  "searched. Use --recheck to search them again.")

    if not queue:
        print("\n  Every qualified company has already been searched.")
        print("  Use --recheck to redo them, or qualify more companies first.\n")
        return 0

    seen_keys = existing_contact_keys(existing)
    next_number = int(next_id(existing, "Contact ID", "P")[1:])

    # Back up before a bulk change: the workbook is the only copy of
    # your research, and a long run that goes wrong should be recoverable.
    from app.excel import backup

    saved = backup(config.excel_path, "contacts")
    if saved:
        logger.info("Backed up the workbook to %s", saved.name)

    print(f"Searching for contacts at {len(queue)} company/companies...\n")

    new_rows: list[dict] = []
    saved_total = 0
    save_every = 5  # flush to the workbook every few companies

    for index, company in enumerate(queue):
        name = str(company.get("Company Name", "?"))

        if index > 0 and not args.mock and config.seconds_between_ai_calls:
            time.sleep(config.seconds_between_ai_calls)

        # Step 1: Python finds real contacts on real pages. No AI.
        pack = research_company(name, str(company.get("Website", "")))
        search = find_contacts(company, pack)

        if not search.found_anything:
            reason = search.notes[0] if search.notes else "nothing published"
            print(f"  {name[:38]:<40} none found -- {reason[:44]}")

            # Record the attempt, so this company is not searched again on
            # every future run. It is a real row with no email, which the
            # rest of the pipeline already knows how to ignore.
            new_rows.append({
                "Contact ID": f"P{next_number:03d}",
                "Company ID": company.get("Company ID", schema.UNKNOWN),
                "Company Name": name,
                "Contact Name": schema.UNKNOWN,
                "Contact Role": schema.UNKNOWN,
                "Contact Type": schema.UNKNOWN,
                "Email": schema.UNKNOWN,
                "Email Verified": "NO",
                "LinkedIn": schema.UNKNOWN,
                "Source": schema.UNKNOWN,
                "Why This Contact": f"Searched on {today()}: {reason}"[:500],
                "Contact Status": "NONE_FOUND",
            })
            next_number += 1
            continue

        # Step 2: the AI labels what was found. It cannot add anything.
        search = classify_contacts(config, company, search, ai_caller)

        rows = build_contact_rows(search, company, next_number, limit=args.per_company)

        # Duplicate prevention, per your specification.
        fresh = []
        for row in rows:
            email = str(row["Email"]).strip().lower()
            key = (
                str(row["Company ID"]),
                email if email != schema.UNKNOWN.lower() else str(row["Source"]).lower(),
            )
            if key in seen_keys:
                logger.info("%s: skipping contact already recorded (%s)", name, email)
                continue
            seen_keys.add(key)
            fresh.append(row)

        # Re-number after de-duplication so IDs stay contiguous.
        for row in fresh:
            row["Contact ID"] = f"P{next_number:03d}"
            next_number += 1

        new_rows.extend(fresh)
        for row in fresh:
            shown = row["Email"] if row["Email"] != schema.UNKNOWN else row["Source"]
            print(f"  {name[:38]:<40} {str(row['Contact Type'])[:28]:<30} {shown[:44]}")

        # Save as we go. This command can run for many minutes across
        # dozens of companies, and writing only at the end meant an
        # interruption threw away everything found so far.
        if fresh and len(new_rows) >= save_every:
            try:
                append_rows(config.excel_path, schema.CONTACTS, new_rows)
                saved_total += len(new_rows)
                logger.info("Saved %d contact(s) so far.", saved_total)
                new_rows = []
            except ExcelError as exc:
                logger.error("Could not save progress: %s", exc)
                return 1

    if not new_rows:
        print("\nNo new contacts found.\n")
        return 0

    try:
        append_rows(config.excel_path, schema.CONTACTS, new_rows)
    except ExcelError as exc:
        logger.error("Contacts could NOT be saved: %s", exc)
        return 1

    print(f"\nAdded {len(new_rows)} contact(s) to the Contacts sheet.")
    print("Every email was observed on the company's own website -- none were guessed.")
    print("\nNext: python main.py show-contacts\n")
    return 0


def cmd_show_contacts(config: Config, args) -> int:
    """List the contacts found so far."""
    try:
        contacts = read_contacts(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not contacts:
        print("\nNo contacts yet. Run: python main.py find-contacts\n")
        return 0

    print()
    print(f"{'ID':<6} {'Company':<26} {'Type':<32} {'Email / route'}")
    print("-" * 108)
    for contact in contacts:
        email = str(contact.get("Email", ""))
        route = email if email and email != schema.UNKNOWN else str(contact.get("Source", ""))
        print(
            f"{str(contact.get('Contact ID','')):<6} "
            f"{str(contact.get('Company Name',''))[:25]:<26} "
            f"{str(contact.get('Contact Type',''))[:31]:<32} "
            f"{route[:42]}"
        )
    print("-" * 108)
    print(f"{len(contacts)} contacts\n")
    return 0


def cmd_draft(config: Config, args) -> int:
    """Generate email drafts for contacts at qualified companies.

    Every draft is written with Approval Status = PENDING. Nothing is
    sent, and there is no code path in this command that can send.
    """
    from app.email_drafts import (
        DraftError,
        already_contacted_companies,
        build_signature,
        existing_outreach_keys,
        generate_draft,
        strip_trailing_name,
        to_outreach_row,
    )
    from app.excel import next_id, today, read_settings, today

    if args.mock:
        print("\nMOCK MODE -- drafts are fake placeholder text.\n")
    elif not config.has_ai_key:
        logger.error("No AI API key configured. Add AI_API_KEY to .env, or use --mock.")
        return 1

    try:
        ai_caller = get_ai_caller(config, use_mock=args.mock)
    except AIError as exc:
        logger.error("%s", exc)
        return 1

    try:
        companies = {c["Company ID"]: c for c in read_companies(config.excel_path)}
        contacts = read_contacts(config.excel_path)
        outreach = read_outreach(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not contacts:
        print("\nNo contacts yet. Run: python main.py find-contacts\n")
        return 0

    # Warn about profile fields the email would otherwise quote.
    missing = [k for k, v in settings.items() if str(v).strip() == "FILL_IN"]
    if missing:
        logger.warning(
            "These Settings are still FILL_IN and will be left out of the "
            "signature: %s", ", ".join(missing),
        )

    seen = existing_outreach_keys(outreach)
    # Companies already written to, whatever the campaign label. This
    # catches applications logged with `log-application`, which carry a
    # different campaign and used to slip past the campaign-level check.
    contacted = already_contacted_companies(outreach)
    next_number = int(next_id(outreach, "Outreach ID", "O")[1:])
    signature = build_signature(settings)

    # One draft per company, aimed at its best contact. The contacts are
    # already ranked, so the first one for a company is the best route.
    best_contact: dict[str, dict] = {}
    for contact in contacts:
        company_id = str(contact.get("Company ID", "")).strip()
        if company_id not in best_contact:
            best_contact[company_id] = contact

    queue = []
    skipped_contacted: list[tuple[str, str]] = []
    for company_id, contact in best_contact.items():
        company = companies.get(company_id)
        if not company:
            logger.warning("Contact %s refers to unknown company %s -- skipping.",
                           contact.get("Contact ID"), company_id)
            continue
        status = str(company.get("Research Status", "")).strip().upper()
        if status != schema.RESEARCH_QUALIFY and not args.include_review:
            continue

        # Do not write to a company you have already contacted, however
        # that contact was made.
        if company_id in contacted and not args.again:
            skipped_contacted.append(
                (str(company.get("Company Name", "?")), contacted[company_id])
            )
            continue

        queue.append((company, contact))

    if skipped_contacted:
        print(f"\n  Skipping {len(skipped_contacted)} company/companies you have "
              "already contacted:")
        for name, why in skipped_contacted[:12]:
            print(f"    {name[:34]:<36} {why}")
        if len(skipped_contacted) > 12:
            print(f"    ... and {len(skipped_contacted) - 12} more")
        print("  Use --again to draft for them anyway.")

    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print(
            "\nNothing to draft. Contacts exist only for companies that are not "
            "QUALIFY.\nUse --include-review to draft for NEEDS_REVIEW companies too.\n"
        )
        return 0

    # Back up before a bulk change: the workbook is the only copy of
    # your research, and a long run that goes wrong should be recoverable.
    from app.excel import backup

    saved = backup(config.excel_path, "draft")
    if saved:
        logger.info("Backed up the workbook to %s", saved.name)

    print(f"Drafting {len(queue)} email(s)...\n")

    new_rows: list[dict] = []
    flagged = 0
    for index, (company, contact) in enumerate(queue):
        name = str(company.get("Company Name", "?"))

        if index > 0 and not args.mock and config.seconds_between_ai_calls:
            time.sleep(config.seconds_between_ai_calls)

        try:
            draft, campaign, problems = generate_draft(
                config, company, settings, ai_caller
            )
        except DraftError as exc:
            logger.error("%s", exc)
            continue

        key = (
            str(company.get("Company ID", "")),
            str(contact.get("Contact ID", "")),
            campaign,
        )
        if key in seen:
            logger.info("%s: already drafted for this campaign -- skipping.", name)
            continue
        seen.add(key)

        # Append the signature here rather than asking the AI to write
        # it: the details are facts from Settings and must not be
        # paraphrased or invented.
        # The model sometimes signs off with the student's name despite
        # being told not to; strip it so the signature does not repeat it.
        body = strip_trailing_name(draft.body, str(settings.get("Name", "")))
        draft.body = f"{body.rstrip()}\n\n{signature}"

        row = to_outreach_row(
            f"O{next_number:03d}", company, contact, draft, campaign,
            problems, today(),
        )
        next_number += 1
        new_rows.append(row)

        marker = "  FLAGGED" if problems else ""
        if problems:
            flagged += 1
        print(f"  {name[:34]:<36} {campaign[:34]:<36}{marker}")
        for problem in problems:
            print(f"      ! {problem}")

    if not new_rows:
        print("\nNo new drafts created.\n")
        return 0

    try:
        append_rows(config.excel_path, schema.OUTREACH, new_rows)
    except ExcelError as exc:
        logger.error("Drafts could NOT be saved: %s", exc)
        return 1

    print(f"\nCreated {len(new_rows)} draft(s), all PENDING. Nothing has been sent.")
    if flagged:
        print(f"{flagged} draft(s) were flagged for quality -- see the Notes column.")
    print("\nNext: python main.py show-drafts")
    print("Then review them in Excel and set Approval Status to APPROVED.\n")
    return 0


def cmd_log_application(config: Config, args) -> int:
    """Record an application you made outside this system.

    Real job hunting does not happen only inside one tool. If you applied
    through a company's portal, or emailed someone before drafting
    anything here, this records it so the workbook stays the honest
    source of truth -- and so follow-up tracking and duplicate prevention
    still work for that company.
    """
    from app.excel import next_id, today, today, update_row

    try:
        companies = read_companies(config.excel_path)
        contacts = read_contacts(config.excel_path)
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    needle = args.company.strip().lower()
    matches = [
        c for c in companies
        if needle in str(c.get("Company Name", "")).strip().lower()
    ]

    if not matches:
        logger.error("No company matching %r. Try `python main.py show-companies`.",
                     args.company)
        return 1
    if len(matches) > 1:
        logger.error(
            "%r matches %d companies: %s. Be more specific.",
            args.company, len(matches),
            ", ".join(str(c.get("Company Name")) for c in matches[:5]),
        )
        return 1

    company = matches[0]
    company_id = str(company.get("Company ID", ""))
    name = str(company.get("Company Name", ""))

    # Already logged? Do not create a second record for the same company.
    existing = [
        r for r in outreach
        if str(r.get("Company ID", "")).strip() == company_id
    ]
    if existing and not args.again:
        row = existing[0]
        print(f"\n  {name} already has an outreach record "
              f"({row.get('Outreach ID')}, {row.get('Email Status')}).")
        print("  Use --again if you really did contact them a second time.\n")
        return 0

    # Use a known contact if there is one, so replies can be matched later.
    company_contacts = [
        c for c in contacts
        if str(c.get("Company ID", "")).strip() == company_id
    ]
    contact = company_contacts[0] if company_contacts else {}

    when = args.date or today()
    outreach_id = next_id(outreach, "Outreach ID", "O")

    record = {
        "Outreach ID": outreach_id,
        "Company ID": company_id,
        "Contact ID": contact.get("Contact ID", schema.UNKNOWN),
        "Company Name": name,
        "Contact Name": contact.get("Contact Name", schema.UNKNOWN),
        "Campaign": args.campaign or "Applied outside the system",
        "Subject": args.subject or f"Application to {name}",
        "Email Body": (
            "Recorded manually. This application was made outside this system "
            f"({args.how}), so the exact text is not stored here."
        ),
        # It genuinely went out, so it is approved and sent.
        "Approval Status": schema.APPROVAL_APPROVED,
        "Email Status": schema.EMAIL_SENT,
        "Date Drafted": when,
        "Date Approved": when,
        "Date Sent": when,
        "Reply Status": schema.REPLY_NONE,
        "Notes": f"Logged manually: {args.how}",
    }

    try:
        append_rows(config.excel_path, schema.OUTREACH, [record])
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  Recorded {outreach_id}: {name}, {args.how}, on {when}.")
    print("  Follow-ups for this company will now be tracked from that date.")
    print("  It will not be drafted again.\n")
    return 0


def cmd_show_drafts(config: Config, args) -> int:
    """Print the drafts so you can read them without opening Excel."""
    try:
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not outreach:
        print("\nNo drafts yet. Run: python main.py draft\n")
        return 0

    if args.full:
        for row in outreach:
            print("\n" + "=" * 72)
            print(f"{row.get('Outreach ID')}  {row.get('Company Name')}")
            print(f"Campaign : {row.get('Campaign')}")
            print(f"Status   : {row.get('Approval Status')} / {row.get('Email Status')}")
            if row.get("Notes"):
                print(f"NOTES    : {row.get('Notes')}")
            print("-" * 72)
            print(f"Subject: {row.get('Subject')}\n")
            print(row.get("Email Body"))
        print("\n" + "=" * 72)
        print(f"{len(outreach)} draft(s)\n")
        return 0

    print()
    print(f"{'ID':<6} {'Company':<26} {'Approval':<10} {'Email':<10} Subject")
    print("-" * 100)
    for row in outreach:
        print(
            f"{str(row.get('Outreach ID','')):<6} "
            f"{str(row.get('Company Name',''))[:25]:<26} "
            f"{str(row.get('Approval Status','')):<10} "
            f"{str(row.get('Email Status','')):<10} "
            f"{str(row.get('Subject',''))[:44]}"
        )
    print("-" * 100)
    print(f"{len(outreach)} draft(s). Use --full to read them.\n")
    return 0


def cmd_review(config: Config, args) -> int:
    """Walk through pending drafts one at a time in the terminal.

    Excel is still the source of truth -- this writes the same columns
    you would edit by hand. It exists because reading a long email in a
    spreadsheet cell is unpleasant, not because Excel is inadequate.
    """
    from app.approval import apply_decision
    from app.excel import update_row

    try:
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    pending = [
        r for r in outreach
        if str(r.get("Approval Status", "")).strip().upper() == schema.APPROVAL_PENDING
    ]

    if not pending:
        print("\nNothing is PENDING review.")
        print("Run `python main.py check-approvals` to see the current state.\n")
        return 0

    print(f"\n{len(pending)} draft(s) to review.")
    print("For each one: [a]pprove  [r]eject  [s]kip  [q]uit\n")

    approved = rejected = skipped = 0

    for index, draft in enumerate(pending, start=1):
        print("=" * 72)
        print(f"({index}/{len(pending)})  {draft.get('Outreach ID')}  "
              f"{draft.get('Company Name')}")
        print(f"Campaign : {draft.get('Campaign')}")
        if draft.get("Notes"):
            print(f"\n!! {draft.get('Notes')}\n")
        print("-" * 72)
        print(f"Subject: {draft.get('Subject')}\n")
        print(draft.get("Email Body"))
        print("-" * 72)

        try:
            answer = input("[a]pprove  [r]eject  [s]kip  [q]uit > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            break

        if answer.startswith("q"):
            print("Stopped.")
            break
        if answer.startswith("a"):
            decision = schema.APPROVAL_APPROVED
            approved += 1
        elif answer.startswith("r"):
            decision = schema.APPROVAL_REJECTED
            rejected += 1
        else:
            skipped += 1
            print("Skipped -- still PENDING.\n")
            continue

        try:
            update_row(
                config.excel_path, schema.OUTREACH, draft["_row"],
                apply_decision(draft, decision),
            )
            print(f"Set to {decision}.\n")
        except ExcelError as exc:
            logger.error("Could not save: %s", exc)
            return 1

    print(f"\nApproved {approved}, rejected {rejected}, skipped {skipped}.")
    print("Run `python main.py check-approvals` to confirm what would send.\n")
    return 0


def _warn_if_open_in_excel(config: Config) -> None:
    """Warn when the workbook looks open in Excel.

    Excel keeps edits in memory until you save, so a command can read a
    file that does not yet contain the approvals you just typed. The
    lock file ~$name.xlsx exists while the workbook is open.
    """
    lock = config.excel_path.parent / f"~${config.excel_path.name}"
    if lock.exists():
        logger.warning(
            "The workbook is OPEN in Excel right now. This causes two problems:\n"
            "           1. Edits you have not saved are invisible to this command.\n"
            "           2. Rows this command adds will NOT appear in Excel until\n"
            "              you close the workbook and reopen it.\n"
            "         Close Excel (saying No to 'Save changes?' if the file has\n"
            "         been updated underneath you), then reopen it to see changes."
        )


def cmd_check_approvals(config: Config, args) -> int:
    """Show exactly what would be sent, and why everything else would not.

    Run this before Stage 8 sending. It uses the same is_sendable()
    function the sender will use, so what it reports is what will happen.
    """
    from app.approval import summarise

    _warn_if_open_in_excel(config)

    try:
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not outreach:
        print("\nNo drafts yet. Run: python main.py draft\n")
        return 0

    result = summarise(outreach)

    print(f"\n{'='*66}\n  APPROVAL STATUS\n{'='*66}\n")
    print(f"  {len(outreach)} outreach row(s)\n")
    for status, count in sorted(result["counts"].items()):
        print(f"    {status:<12}: {count}")

    if result["invalid"]:
        print(f"\n  PROBLEMS -- these values are not recognised:\n")
        for row, reason in result["invalid"]:
            print(f"    {row.get('Outreach ID')}  {row.get('Company Name')[:28]}")
            print(f"        {reason}")

    if not result["sendable"]:
        pending = result["counts"].get(schema.APPROVAL_PENDING, 0)
        if pending:
            print(f"\n  Nothing is approved yet ({pending} still PENDING).")
            print("\n  To approve:")
            print("      python main.py review          (in the terminal)")
            print("      or type APPROVED in the Approval Status column in Excel,")
            print("      then press Cmd+S and CLOSE the file before running commands.")

    print(f"\n  WOULD BE SENT: {len(result['sendable'])}\n")
    for row in result["sendable"]:
        print(f"    {row.get('Outreach ID')}  {str(row.get('Company Name'))[:30]:<32} "
              f"{str(row.get('Subject'))[:40]}")

    if result["blocked"]:
        print(f"\n  WOULD NOT BE SENT: {len(result['blocked'])}\n")
        for row, reason in result["blocked"]:
            print(f"    {row.get('Outreach ID')}  {str(row.get('Company Name'))[:28]:<30} "
                  f"{reason[:44]}")

    print()
    if result["sendable"]:
        print("  Nothing has been sent -- sending is Stage 8 and does not exist yet.")
    print()
    return 0


def cmd_export(config: Config, args) -> int:
    """Export approved emails to a text file for manual sending.

    Only rows the approval gate permits are exported, so sending by hand
    obeys exactly the same rule as automated sending would.
    """
    from app.excel import read_settings
    from app.manual_send import build_export, write_export_file

    try:
        outreach = read_outreach(config.excel_path)
        contacts = read_contacts(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    outreach = _filter_rows(outreach, args)
    by_email, by_portal = build_export(outreach, contacts, settings)

    if not by_email and not by_portal:
        print("\nNothing is approved yet.")
        print("Approve drafts with `python main.py review`, then run this again.\n")
        return 0

    path = Path(args.output) if args.output else config.excel_path.parent / "to_send.txt"
    from_address = str(settings.get("Email", "")).strip() or "(set Email in Settings)"

    try:
        written = write_export_file(path, by_email, by_portal, from_address)
    except OSError as exc:
        logger.error("Could not write %s: %s", path, exc)
        return 1

    print(f"\nWrote {written}")
    print()
    if by_email:
        print(f"  {len(by_email)} email(s) to send from {from_address}:")
        for entry in by_email:
            print(f"    {entry['outreach_id']}  {entry['company'][:28]:<30} -> {entry['target']}")
    if by_portal:
        print(f"\n  {len(by_portal)} application(s) with no email address:")
        for entry in by_portal:
            print(f"    {entry['outreach_id']}  {entry['company'][:28]:<30} -> {entry['target'][:40]}")

    print("\nAfter sending each one, record it:")
    print("    python main.py mark-sent O001\n")
    return 0


def cmd_mark_sent(config: Config, args) -> int:
    """Record that you sent an approved email by hand.

    Refuses to mark anything that was not approved, so the workbook
    cannot claim an unapproved email went out.
    """
    from app.approval import is_sendable
    from app.excel import today, update_row
    from app.manual_send import mark_sent_updates

    try:
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    wanted = {i.strip().upper() for i in args.outreach_ids}
    matched = [
        r for r in outreach
        if str(r.get("Outreach ID", "")).strip().upper() in wanted
    ]

    found_ids = {str(r.get("Outreach ID", "")).strip().upper() for r in matched}
    for missing in sorted(wanted - found_ids):
        logger.error("No outreach row with ID %s.", missing)

    if not matched:
        return 1

    marked = 0
    for row in matched:
        outreach_id = row.get("Outreach ID")

        # Already sent? Say so rather than overwriting the date.
        if str(row.get("Email Status", "")).strip().upper() == schema.EMAIL_SENT:
            print(f"  {outreach_id}: already marked as sent on {row.get('Date Sent')}.")
            continue

        allowed, reason = is_sendable(row)
        if not allowed:
            if not args.force:
                logger.error(
                    "%s was not approved (%s). Not marking it as sent.\n"
                    "         If you really did send this email by hand, "
                    "re-run with --force.",
                    outreach_id, reason,
                )
                continue
            # --force exists for one honest case: you sent the email
            # yourself before approving it in the workbook. Recording
            # reality is better than a workbook that disagrees with your
            # sent folder. We still approve the row rather than leaving
            # it inconsistent, and note that it happened.
            logger.warning(
                "%s was not approved (%s). Recording it as sent anyway "
                "because --force was given.", outreach_id, reason,
            )

        updates = mark_sent_updates(today(), note=args.note or "")
        if not allowed and args.force:
            # Keep the two status columns consistent with what happened.
            updates["Approval Status"] = schema.APPROVAL_APPROVED
            updates["Date Approved"] = today()
            existing_note = updates.get("Notes", "")
            marker = "Sent manually before approval; recorded with --force."
            updates["Notes"] = f"{existing_note} {marker}".strip()

        try:
            update_row(config.excel_path, schema.OUTREACH, row["_row"], updates)
        except ExcelError as exc:
            logger.error("Could not save: %s", exc)
            return 1

        print(f"  {outreach_id}: marked SENT on {today()}  ({row.get('Company Name')})")
        marked += 1

    if marked:
        print(f"\nRecorded {marked} sent email(s).")
        print("Follow-ups will be tracked from these dates in Stage 10.\n")
    return 0


def cmd_followups(config: Config, args) -> int:
    """Show which follow-ups are due, and optionally draft them.

    Works whether you send by hand or through Gmail, because it reads
    Date Sent from the workbook rather than from any mail provider.
    """
    from datetime import date

    from app.excel import read_settings, update_row
    from app.followups import (
        FollowUpError,
        find_due,
        generate_followup,
        record_followup_updates,
    )

    try:
        outreach = read_outreach(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not outreach:
        print("\nNo outreach rows yet. Run: python main.py draft\n")
        return 0

    schedule = {
        1: config.followup_1_days,
        2: config.followup_2_days,
        3: config.followup_3_days,
    }

    due, upcoming, stopped = find_due(
        outreach, today=date.today(), schedule=schedule, include_upcoming=True
    )

    print(f"\n{'='*68}\n  FOLLOW-UPS\n{'='*68}\n")
    print(f"  Schedule: day {schedule[1]}, day {schedule[2]}, day {schedule[3]} "
          f"after sending. Maximum 3.\n")

    if due:
        print(f"  DUE NOW ({len(due)}):\n")
        for item in due:
            overdue = (
                "due today" if item.days_overdue == 0
                else f"{item.days_overdue} day(s) overdue"
            )
            print(f"    {item.outreach_row.get('Outreach ID')}  "
                  f"{str(item.outreach_row.get('Company Name'))[:28]:<30} "
                  f"{item.label:<16} {overdue}")
    else:
        print("  Nothing is due today.\n")

    if upcoming:
        print(f"\n  UPCOMING ({len(upcoming)}):\n")
        for item in upcoming:
            print(f"    {item.outreach_row.get('Outreach ID')}  "
                  f"{str(item.outreach_row.get('Company Name'))[:28]:<30} "
                  f"{item.label:<16} due {item.due_date} "
                  f"(in {-item.days_overdue} day(s))")

    if stopped:
        print(f"\n  SEQUENCE STOPPED ({len(stopped)}):\n")
        for row, reason in stopped:
            print(f"    {row.get('Outreach ID')}  "
                  f"{str(row.get('Company Name'))[:28]:<30} {reason}")

    if not args.draft:
        print()
        if due:
            print("  To draft these follow-ups:  python main.py followups --draft\n")
        return 0

    # --- Drafting ---------------------------------------------------------

    if not due:
        print("\n  Nothing to draft.\n")
        return 0

    if args.mock:
        print("\n  MOCK MODE -- drafts are fake.\n")
    elif not config.has_ai_key:
        logger.error("No AI API key configured. Add AI_API_KEY to .env, or use --mock.")
        return 1

    try:
        ai_caller = get_ai_caller(config, use_mock=args.mock)
    except AIError as exc:
        logger.error("%s", exc)
        return 1

    from app.email_drafts import build_signature, strip_trailing_name

    signature = build_signature(settings)
    drafted = 0

    print(f"\n  Drafting {len(due)} follow-up(s)...\n")

    for index, item in enumerate(due):
        row = item.outreach_row
        company = str(row.get("Company Name", "?"))

        if index > 0 and not args.mock and config.seconds_between_ai_calls:
            time.sleep(config.seconds_between_ai_calls)

        sent_on = row.get("Date Sent")
        days_since = (date.today() - item.due_date).days + (
            config.followup_1_days if item.number == 1 else 0
        )

        try:
            draft, problems = generate_followup(
                config, row, item.number, max(days_since, 1), settings, ai_caller
            )
        except FollowUpError as exc:
            logger.error("%s", exc)
            continue

        body = strip_trailing_name(draft.body, str(settings.get("Name", "")))
        full_body = f"{body.rstrip()}\n\n{signature}"

        print("=" * 68)
        print(f"  {row.get('Outreach ID')}  {company}  --  {item.label}")
        print("=" * 68)
        print(f"Subject: {draft.subject}\n")
        print(full_body)
        if problems:
            print("\n  ! QUALITY WARNINGS:")
            for problem in problems:
                print(f"      - {problem}")
        print()
        drafted += 1

    print("=" * 68)
    print(f"\n  Drafted {drafted} follow-up(s). NOTHING has been sent or saved.")
    print("  These are shown for you to copy, edit and send yourself.")
    print("\n  After sending one, record it:")
    print("      python main.py mark-followup O003 1\n")
    return 0


def cmd_mark_followup(config: Config, args) -> int:
    """Record that you sent a follow-up by hand."""
    from app.excel import today, update_row
    from app.followups import FollowUpError, has_replied, record_followup_updates

    try:
        outreach = read_outreach(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    wanted = args.outreach_id.strip().upper()
    matches = [
        r for r in outreach
        if str(r.get("Outreach ID", "")).strip().upper() == wanted
    ]
    if not matches:
        logger.error("No outreach row with ID %s.", wanted)
        return 1

    row = matches[0]

    # Refuse to log a follow-up to someone who already replied.
    if has_replied(row):
        logger.error(
            "%s already received a reply (%s). The follow-up sequence should "
            "have stopped -- not recording this.",
            wanted, row.get("Reply Status"),
        )
        return 1

    try:
        updates = record_followup_updates(
            args.number, today(), notes=str(row.get("Notes", ""))
        )
    except FollowUpError as exc:
        logger.error("%s", exc)
        return 1

    try:
        update_row(config.excel_path, schema.OUTREACH, row["_row"], updates)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  {wanted}: recorded follow-up {args.number} sent {today()}.")
    print("  Run `python main.py followups` to see what is next.\n")
    return 0


def cmd_gmail_auth(config: Config, args) -> int:
    """Authorise Gmail access, or confirm existing authorisation."""
    from app.gmail import GmailError, authenticate, get_profile

    try:
        service = authenticate(force=args.force)
        profile = get_profile(service)
    except GmailError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  Authorised as: {profile.get('emailAddress')}")
    print(f"  Messages in mailbox: {profile.get('messagesTotal')}")
    print(f"  Token saved to: token.json (git-ignored -- never share it)")
    print("\n  Next: send a test email to yourself with")
    print("        python main.py gmail-test\n")
    return 0


def cmd_gmail_test(config: Config, args) -> int:
    """Send one test email to YOURSELF. Never to a company.

    Your specification requires the first live send to go to your own
    address. This command cannot send anywhere else: the recipient is
    read from the Settings sheet and checked against the authorised
    account, so a typo cannot email a stranger.
    """
    from app.excel import read_settings
    from app.gmail import (
        GmailError,
        authenticate,
        build_message,
        get_profile,
        send_message,
    )

    try:
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    own_address = str(settings.get("Email", "")).strip()
    if not own_address or own_address == "FILL_IN":
        logger.error(
            "Your own email is not set in the Settings sheet. "
            "Fill in the Email row before running a test send."
        )
        return 1

    try:
        service = authenticate()
        profile = get_profile(service)
    except GmailError as exc:
        logger.error("%s", exc)
        return 1

    authorised = str(profile.get("emailAddress", "")).strip()

    # The safety interlock: the test can only ever go to the account we
    # are authorised as. There is no way to point this at a company.
    if authorised.lower() != own_address.lower():
        logger.error(
            "Refusing to send. The Settings sheet says your email is %s, but "
            "Gmail is authorised as %s. A test email must go to your own "
            "account. Fix the Email row in Settings, or re-authorise.",
            own_address, authorised,
        )
        return 1

    body = (
        "This is a test message from the internship outreach system.\n\n"
        "If you are reading this, Gmail sending works. No employer has been "
        "contacted by this test.\n\n"
        f"Authorised account: {authorised}\n"
        f"Daily send limit:   {config.daily_send_limit}\n"
        f"Batch size:         {config.batch_size}\n"
        f"Dry run:            {config.dry_run}\n"
    )

    if config.dry_run:
        print("\n  DRY_RUN is true -- nothing was sent.")
        print("  This is what WOULD have been sent:\n")
        print(f"    To:      {own_address}")
        print(f"    Subject: Outreach system test\n")
        print("  Set DRY_RUN=false in .env when you are ready to send it.\n")
        return 0

    try:
        payload = build_message(own_address, "Outreach system test", body)
        message_id = send_message(service, payload)
    except GmailError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  Sent a test email to {own_address}")
    print(f"  Gmail message id: {message_id}")
    print("\n  Check your inbox. If it arrived, sending works.\n")
    return 0


def _filter_rows(rows: list[dict], args) -> list[dict]:
    """Narrow a set of outreach rows by the --only / --since flags.

    Exists so you can send a specific batch instead of everything that is
    approved -- for example just today's drafts, or three named IDs.
    """
    only = getattr(args, "only", None)
    if only:
        wanted = {i.strip().upper() for i in only}
        rows = [r for r in rows
                if str(r.get("Outreach ID", "")).strip().upper() in wanted]

    since = getattr(args, "since", None)
    if since:
        if since.lower() == "today":
            from datetime import date

            since = date.today().isoformat()
        rows = [r for r in rows
                if str(r.get("Date Drafted", "")).strip()[:10] >= since]

    return rows


def cmd_send(config: Config, args) -> int:
    """Send approved emails through Gmail.

    Every guard is enforced here: the approval gate, DRY_RUN, the daily
    limit, the batch size, the delay between sends, and duplicate
    prevention.
    """
    from app.approval import is_sendable
    from app.excel import read_settings, today, update_row
    from app.gmail import (
        GmailError,
        authenticate,
        build_message,
        failed_updates,
        get_profile,
        is_valid_email,
        remaining_today,
        send_message,
        sent_today,
        sent_updates,
    )
    from app.manual_send import SendRoute, resolve_route

    _warn_if_open_in_excel(config)

    try:
        outreach = read_outreach(config.excel_path)
        contacts = read_contacts(config.excel_path)
        settings = read_settings(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    contacts_by_id = {str(c.get("Contact ID", "")).strip(): c for c in contacts}

    # 1. Approval gate. Same function the manual path uses.
    approved = []
    for row in outreach:
        allowed, reason = is_sendable(row)
        if allowed:
            approved.append(row)

    # 2. Optional narrowing, so you can send a specific batch rather than
    #    everything that happens to be approved.
    approved = _filter_rows(approved, args)
    if not approved:
        print("\n  Nothing matches that filter.\n")
        return 0

    if not approved:
        print("\n  Nothing is approved and unsent.")
        print("  Run `python main.py check-approvals` to see why.\n")
        return 0

    # 3. Split by route. Portal applications cannot be emailed.
    sendable = []
    portal_only = []
    for row in approved:
        route, target = resolve_route(row, contacts_by_id)
        if route == SendRoute.EMAIL and is_valid_email(target):
            sendable.append((row, target))
        else:
            portal_only.append((row, target))

    # 4. Daily limit and batch size.
    already = sent_today(outreach)
    remaining = remaining_today(config, outreach)
    batch = min(config.batch_size, remaining, len(sendable))
    if args.limit is not None:
        batch = min(batch, args.limit)

    print(f"\n{'='*66}\n  SENDING\n{'='*66}\n")
    print(f"  Approved and unsent : {len(approved)}")
    print(f"  Can be emailed      : {len(sendable)}")
    if portal_only:
        print(f"  Portal applications : {len(portal_only)}  (apply by hand, "
              "cannot be emailed)")
    print(f"  Already sent today  : {already} of {config.daily_send_limit}")
    print(f"  Will send now       : {batch}")
    print(f"  Dry run             : {config.dry_run}")
    print()

    if remaining <= 0:
        print(f"  Daily limit of {config.daily_send_limit} reached. "
              "Try again tomorrow, or raise DAILY_SEND_LIMIT in .env.\n")
        return 0

    if batch <= 0:
        print("  Nothing to send by email right now.\n")
        for row, target in portal_only:
            print(f"    {row.get('Outreach ID')}  "
                  f"{str(row.get('Company Name'))[:28]:<30} apply at {str(target)[:40]}")
        print()
        return 0

    if config.dry_run:
        print("  DRY_RUN is true -- nothing will be sent. These would go out:\n")
        for row, target in sendable[:batch]:
            print(f"    {row.get('Outreach ID')}  "
                  f"{str(row.get('Company Name'))[:26]:<28} -> {target}")
        print("\n  Set DRY_RUN=false in .env to send for real.\n")
        return 0

    # 5. Confirm before sending to real people.
    if not args.yes:
        print("  These emails will be sent to real people:\n")
        for row, target in sendable[:batch]:
            print(f"    {row.get('Outreach ID')}  "
                  f"{str(row.get('Company Name'))[:26]:<28} -> {target}")
        try:
            answer = input("\n  Type SEND to confirm: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.\n")
            return 0
        if answer != "SEND":
            print("  Cancelled -- nothing was sent.\n")
            return 0

    try:
        service = authenticate()
        profile = get_profile(service)
    except GmailError as exc:
        logger.error("%s", exc)
        return 1

    from_address = str(profile.get("emailAddress", ""))
    print(f"\n  Sending as {from_address}\n")

    sent = failed = 0
    for index, (row, target) in enumerate(sendable[:batch]):
        outreach_id = row.get("Outreach ID")
        company = str(row.get("Company Name", "?"))

        # Delay between sends, so a run does not look like a burst.
        if index > 0 and config.seconds_between_sends:
            time.sleep(config.seconds_between_sends)

        try:
            payload = build_message(
                target, str(row.get("Subject", "")), str(row.get("Email Body", "")),
            )
            message_id = send_message(service, payload)
        except GmailError as exc:
            logger.error("%s (%s): %s", outreach_id, company, exc)
            try:
                update_row(config.excel_path, schema.OUTREACH, row["_row"],
                           failed_updates(str(exc)))
            except ExcelError as save_exc:
                logger.error("Could not record the failure: %s", save_exc)
            failed += 1
            continue

        try:
            update_row(config.excel_path, schema.OUTREACH, row["_row"],
                       sent_updates(message_id, today()))
        except ExcelError as exc:
            # The email HAS gone. Say so loudly -- an unrecorded send
            # could be repeated tomorrow.
            logger.error(
                "%s was SENT to %s but could not be recorded in the workbook "
                "(%s). Set Email Status to SENT by hand to avoid sending it "
                "again.", outreach_id, target, exc,
            )

        print(f"    sent  {outreach_id}  {company[:30]:<32} -> {target}")
        sent += 1

    print(f"\n  Sent {sent}, failed {failed}.")
    print(f"  Today's total: {sent_today(read_outreach(config.excel_path))} "
          f"of {config.daily_send_limit}\n")
    return 0


def cmd_check_replies(config: Config, args) -> int:
    """Search Gmail for replies, classify them, and update the workbook.

    Recording a reply automatically stops that thread's follow-up
    sequence, because followups.has_replied() reads the Reply Status
    column this writes.
    """
    from app.excel import update_row
    from app.gmail import GmailError, authenticate
    from app.replies import (
        ReplyError,
        classify_reply,
        fetch_replies,
        match_to_outreach,
        reply_updates,
    )

    try:
        outreach = read_outreach(config.excel_path)
        contacts = read_contacts(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    sent_rows = [
        r for r in outreach
        if str(r.get("Email Status", "")).strip().upper() == schema.EMAIL_SENT
    ]
    if not sent_rows:
        print("\n  No emails have been sent yet, so there is nothing to check.\n")
        return 0

    # Only look for replies from people we actually emailed.
    sent_contact_ids = {str(r.get("Contact ID", "")).strip() for r in sent_rows}
    addresses = [
        str(c.get("Email", "")).strip()
        for c in contacts
        if str(c.get("Contact ID", "")).strip() in sent_contact_ids
    ]
    addresses = [a for a in addresses if a and a.upper() != schema.UNKNOWN]

    if not addresses:
        print("\n  Emails were sent, but none of them have a contact email "
              "address recorded,\n  so there is nothing to search for.\n")
        return 0

    try:
        service = authenticate()
    except GmailError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n  Checking {len(addresses)} address(es) for replies "
          f"(last {args.days} days)...\n")

    try:
        replies = fetch_replies(service, addresses, days=args.days)
    except ReplyError as exc:
        logger.error("%s", exc)
        return 1

    if not replies:
        print("  No replies found.\n")
        return 0

    matched = match_to_outreach(replies, outreach, contacts)
    print(f"  Found {len(replies)} message(s), {len(matched)} matched to outreach.\n")

    if not matched:
        print("  None matched a sent email. Check your inbox by hand.\n")
        return 0

    # Classification needs the AI, but detection above did not -- so you
    # already know replies exist even if this part fails.
    if args.mock:
        print("  MOCK MODE -- classifications are fake.\n")
    elif not config.has_ai_key:
        logger.error("No AI API key configured. Add AI_API_KEY to .env, or use --mock.")
        return 1

    try:
        ai_caller = get_ai_caller(config, use_mock=args.mock)
    except AIError as exc:
        logger.error("%s", exc)
        return 1

    updated = 0
    for index, (reply, row) in enumerate(matched):
        outreach_id = row.get("Outreach ID")
        company = str(row.get("Company Name", "?"))

        existing = str(row.get("Reply Status", "")).strip().upper()
        if existing and existing != schema.REPLY_NONE and not args.force:
            print(f"    {outreach_id}  {company[:26]:<28} already recorded "
                  f"({existing}) -- skipping")
            continue

        if index > 0 and not args.mock and config.seconds_between_ai_calls:
            time.sleep(config.seconds_between_ai_calls)

        result = classify_reply(config, reply, row, ai_caller)

        try:
            update_row(
                config.excel_path, schema.OUTREACH, row["_row"],
                reply_updates(reply, result, str(row.get("Notes", ""))),
            )
        except ExcelError as exc:
            logger.error("Could not save the reply for %s: %s", outreach_id, exc)
            continue

        updated += 1
        print("=" * 68)
        print(f"  {outreach_id}  {company}")
        print(f"  From      : {reply.from_address}")
        print(f"  Received  : {reply.received}")
        print(f"  Verdict   : {result.classification}  "
              f"(confidence {result.confidence:.0%})")
        print(f"  Summary   : {result.summary}")
        print(f"  DO NEXT   : {result.next_action}")
        if result.referred_to and result.referred_to != schema.UNKNOWN:
            print(f"  Referred  : {result.referred_to}")
        if result.deadline_mentioned and result.deadline_mentioned != schema.UNKNOWN:
            print(f"  Deadline  : {result.deadline_mentioned}")
        print()

    print("=" * 68)
    print(f"\n  Recorded {updated} reply/replies.")
    if updated:
        print("  Follow-ups for these threads have stopped automatically.")
    print("\n  Next: python main.py followups\n")
    return 0


def cmd_report(config: Config, args) -> int:
    """Show the score distribution and pipeline counts.

    This is the Stage 4 reporting requirement: the scoring must be
    transparent, so you can see at a glance how the 100 points were
    spread rather than trusting a single total.
    """
    try:
        companies = read_companies(config.excel_path)
    except ExcelError as exc:
        logger.error("%s", exc)
        return 1

    if not companies:
        print("\nNo companies yet. Run: python main.py load-samples\n")
        return 0

    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for company in companies:
        status = str(company.get("Research Status", "")).strip() or "(blank)"
        by_status[status] = by_status.get(status, 0) + 1
        priority = str(company.get("Priority", "")).strip()
        if priority:
            by_priority[priority] = by_priority.get(priority, 0) + 1

    print(f"\n{'='*54}\n  PIPELINE REPORT\n{'='*54}")
    print(f"\n  Companies: {len(companies)}\n")
    for status in [
        schema.RESEARCH_QUALIFY, schema.RESEARCH_NEEDS_REVIEW,
        schema.RESEARCH_REJECT, schema.RESEARCH_PENDING, schema.RESEARCH_ERROR,
    ]:
        if status in by_status:
            print(f"    {status:<14}: {by_status[status]}")

    if by_priority:
        print("\n  Priority bands")
        for band in ["A", "B", "C", "D", schema.RESEARCH_NEEDS_REVIEW, "REJECT"]:
            if band in by_priority:
                print(f"    {band:<14}: {by_priority[band]}")

    # Average score per dimension, across companies that were actually
    # scored. This is what makes the scoring transparent: if one dimension
    # is always near zero, the prompt or the weighting needs attention.
    dimensions = [
        ("Technology Score", 25), ("Internship Score", 25),
        ("Skills Match Score", 20), ("Maturity Score", 10),
        ("Contactability Score", 10), ("Geographic Score", 10),
    ]
    scored = [
        c for c in companies
        if isinstance(c.get("Total Score"), (int, float)) and c.get("Total Score") != ""
    ]
    if scored:
        print(f"\n  Average scores ({len(scored)} scored companies)")
        for column, maximum in dimensions:
            values = [
                c[column] for c in scored if isinstance(c.get(column), (int, float))
            ]
            if values:
                average = sum(values) / len(values)
                filled = int(round(average / maximum * 20))
                bar = "#" * filled + "." * (20 - filled)
                print(f"    {column:<22} {bar} {average:5.1f}/{maximum}")

        totals = [c["Total Score"] for c in scored]
        print(f"\n    {'TOTAL':<22} {'':<20} {sum(totals)/len(totals):5.1f}/100")

    # Stage counts for the rest of the pipeline, once those sheets fill up.
    try:
        contacts = read_contacts(config.excel_path)
        outreach = read_outreach(config.excel_path)
        print(f"\n  Contacts found   : {len(contacts)}")
        print(f"  Outreach drafted : {len(outreach)}")
    except ExcelError:
        pass

    print()
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
    "export-csv": cmd_export_csv,
    "check-excel": cmd_check_excel,
    "discover": cmd_discover,
    "import-companies": cmd_import_companies,
    "fix-urls": cmd_fix_urls,
    "qualify": cmd_qualify,
    "show-results": cmd_show_results,
    "report": cmd_report,
    "check-replies": cmd_check_replies,
    "gmail-auth": cmd_gmail_auth,
    "gmail-test": cmd_gmail_test,
    "send": cmd_send,
    "followups": cmd_followups,
    "mark-followup": cmd_mark_followup,
    "log-application": cmd_log_application,
    "export": cmd_export,
    "mark-sent": cmd_mark_sent,
    "review": cmd_review,
    "check-approvals": cmd_check_approvals,
    "draft": cmd_draft,
    "show-drafts": cmd_show_drafts,
    "find-contacts": cmd_find_contacts,
    "show-contacts": cmd_show_contacts,
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

    discover_parser = subparsers.add_parser(
        "discover", help="Find new organisations through a real web search."
    )
    discover_parser.add_argument("query", nargs="*", help="What to search for.")
    discover_parser.add_argument(
        "--preset", default=None,
        help="A ready-made search. Run `discover` with no arguments to see them all.",
    )
    discover_parser.add_argument(
        "--all", action="store_true",
        help="Run every preset in turn. Slow, and may hit rate limits.",
    )
    discover_parser.add_argument(
        "--engine", choices=["duckduckgo", "brave"], default="duckduckgo",
        help="duckduckgo (default: no key needed) or brave (needs SEARCH_API_KEY).",
    )
    discover_parser.add_argument(
        "--count", type=int, default=20, help="Results per query (max 20)."
    )
    discover_parser.add_argument("--country", default="Kenya", help="Country column value.")
    discover_parser.add_argument("--region", default="Nairobi", help="Region column value.")
    discover_parser.add_argument("--output", default=None, help="Where to write the CSV.")
    discover_parser.add_argument(
        "--replace", action="store_true",
        help="Overwrite the staging file instead of adding to it.",
    )
    discover_parser.add_argument(
        "--no-screen", action="store_true", help="Skip AI screening of results."
    )

    import_parser = subparsers.add_parser(
        "import-companies", help="Add companies from a CSV file."
    )
    import_parser.add_argument(
        "csv_path", nargs="?", default="data/companies_to_add.csv",
        help="Path to the CSV (default: data/companies_to_add.csv).",
    )
    import_parser.add_argument(
        "--keep", action="store_true",
        help="Do not clear data/discovered.csv after importing it.",
    )
    companies_parser = subparsers.add_parser(
        "show-companies", help="List companies in the workbook."
    )

    csv_parser = subparsers.add_parser(
        "export-csv", help="Write every sheet out as a plain CSV file."
    )
    csv_parser.add_argument("--output", default=None, help="Folder to write into.")
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
    subparsers.add_parser("report", help="Score distribution and pipeline counts.")

    contacts_parser = subparsers.add_parser(
        "find-contacts", help="Find real contact routes for qualified companies."
    )
    contacts_parser.add_argument("--mock", action="store_true", help="Fake the AI labelling.")
    contacts_parser.add_argument("--limit", type=int, default=None, help="Only the first N companies.")
    contacts_parser.add_argument(
        "--include-review", action="store_true",
        help="Also search NEEDS_REVIEW companies, not just QUALIFY.",
    )
    contacts_parser.add_argument(
        "--per-company", type=int, default=3,
        help="Maximum contacts to record per company (default 3).",
    )
    contacts_parser.add_argument(
        "--recheck", action="store_true",
        help="Search companies again even if they have been searched before.",
    )

    subparsers.add_parser("show-contacts", help="List contacts found so far.")

    draft_parser = subparsers.add_parser(
        "draft", help="Generate email drafts (PENDING approval; sends nothing)."
    )
    draft_parser.add_argument("--mock", action="store_true", help="Fake drafts, no AI.")
    draft_parser.add_argument("--limit", type=int, default=None, help="Only the first N.")
    draft_parser.add_argument(
        "--include-review", action="store_true",
        help="Also draft for NEEDS_REVIEW companies.",
    )
    draft_parser.add_argument(
        "--again", action="store_true",
        help="Draft even for companies you have already contacted.",
    )

    subparsers.add_parser(
        "review", help="Review pending drafts one at a time and approve or reject."
    )
    subparsers.add_parser(
        "check-approvals", help="Show what would be sent, and why the rest would not."
    )

    replies_parser = subparsers.add_parser(
        "check-replies", help="Search Gmail for replies and classify them."
    )
    replies_parser.add_argument(
        "--days", type=int, default=90, help="How far back to search (default 90)."
    )
    replies_parser.add_argument("--mock", action="store_true", help="Fake classification.")
    replies_parser.add_argument(
        "--force", action="store_true",
        help="Re-classify replies that are already recorded.",
    )

    auth_parser = subparsers.add_parser(
        "gmail-auth", help="Authorise Gmail access (opens your browser once)."
    )
    auth_parser.add_argument(
        "--force", action="store_true", help="Ignore the saved token and re-authorise."
    )

    subparsers.add_parser(
        "gmail-test", help="Send one test email to YOUR OWN address."
    )

    send_parser = subparsers.add_parser(
        "send", help="Send approved emails through Gmail."
    )
    send_parser.add_argument(
        "--limit", type=int, default=None, help="Send at most N this run."
    )
    send_parser.add_argument(
        "--only", nargs="+", default=None, metavar="ID",
        help="Send only these Outreach IDs, e.g. --only O016 O023.",
    )
    send_parser.add_argument(
        "--since", default=None, metavar="DATE",
        help="Only rows drafted on or after this date (YYYY-MM-DD, or 'today').",
    )
    send_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )

    followups_parser = subparsers.add_parser(
        "followups", help="Show which follow-ups are due; --draft to write them."
    )
    followups_parser.add_argument(
        "--draft", action="store_true", help="Draft the follow-ups that are due."
    )
    followups_parser.add_argument("--mock", action="store_true", help="Fake drafts, no AI.")

    mark_followup_parser = subparsers.add_parser(
        "mark-followup", help="Record that you sent a follow-up by hand."
    )
    mark_followup_parser.add_argument("outreach_id", help="e.g. O003")
    mark_followup_parser.add_argument(
        "number", type=int, choices=[1, 2, 3], help="Which follow-up (1, 2 or 3)."
    )

    log_parser = subparsers.add_parser(
        "log-application",
        help="Record an application you made outside this system.",
    )
    log_parser.add_argument("company", help="Company name, or part of it.")
    log_parser.add_argument(
        "--how", default="applied through their careers portal",
        help="How you applied (default: through their careers portal).",
    )
    log_parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today).")
    log_parser.add_argument("--subject", default=None, help="Subject, if you emailed.")
    log_parser.add_argument("--campaign", default=None, help="Campaign label.")
    log_parser.add_argument(
        "--again", action="store_true",
        help="Log a second application to a company you already contacted.",
    )

    export_parser = subparsers.add_parser(
        "export", help="Write approved emails to a text file for manual sending."
    )
    export_parser.add_argument(
        "--output", default=None,
        help="Where to write the file (default: data/to_send.txt).",
    )
    export_parser.add_argument(
        "--only", nargs="+", default=None, metavar="ID",
        help="Export only these Outreach IDs.",
    )
    export_parser.add_argument(
        "--since", default=None, metavar="DATE",
        help="Only rows drafted on or after this date (YYYY-MM-DD, or 'today').",
    )

    sent_parser = subparsers.add_parser(
        "mark-sent", help="Record that you sent an approved email by hand."
    )
    sent_parser.add_argument(
        "outreach_ids", nargs="+", help="One or more Outreach IDs, e.g. O001 O003."
    )
    sent_parser.add_argument("--note", default=None, help="Optional note for the Notes column.")
    sent_parser.add_argument(
        "--force", action="store_true",
        help="Record a send even if the row was not approved first "
             "(for an email you genuinely sent by hand).",
    )

    drafts_parser = subparsers.add_parser("show-drafts", help="List or read drafts.")
    drafts_parser.add_argument(
        "--full", action="store_true", help="Print the full text of every draft."
    )

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

    # Excel keeps the workbook in memory while it is open. That causes two
    # problems people hit constantly:
    #   - edits you have not saved are invisible to these commands
    #   - rows these commands ADD are invisible in Excel until you reopen
    # So warn once, for every command that touches the workbook.
    if command not in {"status", "init-excel", "gmail-auth"}:
        _warn_if_open_in_excel(config)

    return handler(config, args)


if __name__ == "__main__":
    sys.exit(main())

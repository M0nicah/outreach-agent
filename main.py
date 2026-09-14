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
from app.config import Config, ConfigError, load_config
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
    from app.excel import next_id

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

    seen_keys = existing_contact_keys(existing)
    next_number = int(next_id(existing, "Contact ID", "P")[1:])

    print(f"Searching for contacts at {len(queue)} company/companies...\n")

    new_rows: list[dict] = []
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
        build_signature,
        existing_outreach_keys,
        generate_draft,
        strip_trailing_name,
        to_outreach_row,
    )
    from app.excel import next_id, read_settings, today

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
    for company_id, contact in best_contact.items():
        company = companies.get(company_id)
        if not company:
            logger.warning("Contact %s refers to unknown company %s -- skipping.",
                           contact.get("Contact ID"), company_id)
            continue
        status = str(company.get("Research Status", "")).strip().upper()
        if status != schema.RESEARCH_QUALIFY and not args.include_review:
            continue
        queue.append((company, contact))

    if args.limit:
        queue = queue[: args.limit]

    if not queue:
        print(
            "\nNothing to draft. Contacts exist only for companies that are not "
            "QUALIFY.\nUse --include-review to draft for NEEDS_REVIEW companies too.\n"
        )
        return 0

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


def cmd_check_approvals(config: Config, args) -> int:
    """Show exactly what would be sent, and why everything else would not.

    Run this before Stage 8 sending. It uses the same is_sendable()
    function the sender will use, so what it reports is what will happen.
    """
    from app.approval import summarise

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
    "check-excel": cmd_check_excel,
    "fix-urls": cmd_fix_urls,
    "qualify": cmd_qualify,
    "show-results": cmd_show_results,
    "report": cmd_report,
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

    subparsers.add_parser(
        "review", help="Review pending drafts one at a time and approve or reject."
    )
    subparsers.add_parser(
        "check-approvals", help="Show what would be sent, and why the rest would not."
    )

    export_parser = subparsers.add_parser(
        "export", help="Write approved emails to a text file for manual sending."
    )
    export_parser.add_argument(
        "--output", default=None,
        help="Where to write the file (default: data/to_send.txt).",
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

    return handler(config, args)


if __name__ == "__main__":
    sys.exit(main())

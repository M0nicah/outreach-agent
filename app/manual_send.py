"""Support for sending approved emails by hand.

Automated sending (Stage 8) is not required to use this system. Sending
the first emails yourself is a reasonable choice: you see how real
employers reply before automating anything, and the first messages carry
no risk of a technical mistake.

This module does two things:

1. Exports approved emails in a form you can copy into Gmail, including
   the recipient -- or tells you plainly that there is no email address
   and you must apply through a careers portal instead.
2. Records what you actually sent, so the workbook stays the source of
   truth and follow-up tracking still works.

The approval gate is NOT bypassed here. Export only ever shows rows that
`approval.is_sendable()` permits, so manual sending obeys the same rule
as automated sending would.
"""

import logging
from pathlib import Path
from typing import Any

from app import schema
from app.approval import is_sendable

logger = logging.getLogger(__name__)


class SendRoute:
    """How an approved email can actually reach the organisation."""

    EMAIL = "EMAIL"
    PORTAL = "PORTAL"
    NONE = "NONE"


def resolve_route(
    outreach_row: dict[str, Any], contacts_by_id: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """Work out how to deliver one approved email.

    Returns (route, target). A careers portal is a legitimate outcome,
    not a failure -- for a large organisation it is often the only route
    they accept, and pretending otherwise would waste your time.
    """
    contact = contacts_by_id.get(str(outreach_row.get("Contact ID", "")).strip())
    if not contact:
        return SendRoute.NONE, "No contact record found for this outreach row."

    email = str(contact.get("Email", "")).strip()
    if email and email.upper() != schema.UNKNOWN:
        return SendRoute.EMAIL, email

    source = str(contact.get("Source", "")).strip()
    if source and source.upper() != schema.UNKNOWN:
        return SendRoute.PORTAL, source

    return SendRoute.NONE, "No email address and no application URL recorded."


def build_export(
    outreach: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    settings: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prepare approved emails for manual sending.

    Returns (sendable_by_email, portal_applications). Only rows the
    approval gate permits are included.
    """
    contacts_by_id = {str(c.get("Contact ID", "")).strip(): c for c in contacts}

    by_email: list[dict[str, Any]] = []
    by_portal: list[dict[str, Any]] = []

    for row in outreach:
        allowed, reason = is_sendable(row)
        if not allowed:
            logger.debug("%s skipped: %s", row.get("Outreach ID"), reason)
            continue

        route, target = resolve_route(row, contacts_by_id)
        entry = {
            "outreach_id": row.get("Outreach ID", ""),
            "company": row.get("Company Name", ""),
            "subject": row.get("Subject", ""),
            "body": row.get("Email Body", ""),
            "route": route,
            "target": target,
            "row": row.get("_row"),
        }

        if route == SendRoute.EMAIL:
            by_email.append(entry)
        else:
            by_portal.append(entry)

    return by_email, by_portal


def write_export_file(
    path: Path,
    by_email: list[dict[str, Any]],
    by_portal: list[dict[str, Any]],
    from_address: str,
) -> Path:
    """Write a plain text file you can read and copy from.

    Plain text rather than .eml or HTML: you are going to copy and paste
    this into Gmail, and a format you can read in any editor is more
    useful than one that needs a mail client to open.
    """
    lines: list[str] = []
    lines.append("APPROVED EMAILS TO SEND MANUALLY")
    lines.append("=" * 72)
    lines.append(f"From: {from_address}")
    lines.append("")
    lines.append("Copy each block into Gmail. After sending, record it with:")
    lines.append("    python main.py mark-sent <OUTREACH ID>")
    lines.append("")

    if by_email:
        lines.append("")
        lines.append("#" * 72)
        lines.append(f"# {len(by_email)} EMAIL(S) TO SEND")
        lines.append("#" * 72)
        for entry in by_email:
            lines.append("")
            lines.append("=" * 72)
            lines.append(f"[{entry['outreach_id']}]  {entry['company']}")
            lines.append("=" * 72)
            lines.append(f"To:      {entry['target']}")
            lines.append(f"Subject: {entry['subject']}")
            lines.append("")
            lines.append(entry["body"])
            lines.append("")

    if by_portal:
        lines.append("")
        lines.append("#" * 72)
        lines.append(f"# {len(by_portal)} APPLICATION(S) -- NO EMAIL ADDRESS")
        lines.append("#" * 72)
        lines.append("")
        lines.append("These organisations publish a careers portal rather than an")
        lines.append("address. Apply through the URL. The drafted text is included")
        lines.append("because it usually fits a 'cover letter' or 'message' field.")
        for entry in by_portal:
            lines.append("")
            lines.append("=" * 72)
            lines.append(f"[{entry['outreach_id']}]  {entry['company']}")
            lines.append("=" * 72)
            lines.append(f"Apply at: {entry['target']}")
            lines.append(f"Subject:  {entry['subject']}")
            lines.append("")
            lines.append(entry["body"])
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def mark_sent_updates(today: str, note: str = "") -> dict[str, Any]:
    """Column updates recording that an email was sent by hand."""
    updates = {
        "Email Status": schema.EMAIL_SENT,
        "Date Sent": today,
    }
    if note:
        updates["Notes"] = note
    return updates

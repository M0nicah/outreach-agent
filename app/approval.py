"""The human approval gate.

This module answers exactly one question: may this email be sent?

The rule from the specification, which the whole project depends on:

    If Approval Status != APPROVED, the email must NOT be sent,
    even if Email Status says DRAFTED.

`is_sendable` is the single function that decides. Stage 8's sender must
call it and must not reimplement the logic. Keeping the decision in one
tested place means there is one thing to audit, not several.

Note the asymmetry in how statuses are treated. Approval Status is set by
a HUMAN in Excel and is authoritative. Email Status is set by the SYSTEM
and is merely a record. So an unrecognised Approval Status is refused,
never guessed -- if you typo "APPROVE", the system stops rather than
deciding what you probably meant.
"""

import logging
from typing import Any

from app import schema

logger = logging.getLogger(__name__)

# Statuses that permit sending. Deliberately a set of one.
SENDABLE_APPROVAL = {schema.APPROVAL_APPROVED}

# EDITED means you rewrote the draft yourself but have not yet approved
# it. It is not permission to send.
BLOCKING_REASONS = {
    schema.APPROVAL_PENDING: "still PENDING -- you have not reviewed it yet",
    schema.APPROVAL_REJECTED: "REJECTED -- you decided not to send it",
    schema.APPROVAL_EDITED: (
        "EDITED -- you changed the text but did not approve it. "
        "Set Approval Status to APPROVED when you are ready."
    ),
}


class ApprovalError(Exception):
    """Raised when approval state cannot be determined safely."""


def normalise(value: Any) -> str:
    """Tidy a status typed by hand in Excel.

    Handles the ordinary human variations -- lower case, stray spaces --
    but nothing more. We do not guess at misspellings.
    """
    return str(value or "").strip().upper()


def is_sendable(row: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether one outreach row may be sent.

    Returns (allowed, reason). The reason always explains the decision,
    so a refusal can be reported rather than silently skipped.

    This is the ONLY place that grants permission to send.
    """
    approval = normalise(row.get("Approval Status"))
    email_status = normalise(row.get("Email Status"))
    outreach_id = row.get("Outreach ID", "?")

    # 1. An unrecognised status is refused, not interpreted.
    if approval and approval not in schema.APPROVAL_STATUSES:
        return False, (
            f"Approval Status is {approval!r}, which is not a valid value. "
            f"Use one of: {', '.join(schema.APPROVAL_STATUSES)}."
        )

    # 2. Blank means nobody has looked at it.
    if not approval:
        return False, "Approval Status is blank -- nobody has approved this."

    # 3. The gate itself.
    if approval not in SENDABLE_APPROVAL:
        return False, BLOCKING_REASONS.get(approval, f"Approval Status is {approval}.")

    # 4. Already sent: refuse, so a re-run cannot email someone twice.
    if email_status == schema.EMAIL_SENT:
        return False, "already SENT -- sending again would be a duplicate."

    # 5. Sanity checks on the content itself. An approved-but-empty row
    #    would otherwise send a blank email.
    if not str(row.get("Subject", "")).strip():
        return False, "approved, but the Subject is empty."
    if not str(row.get("Email Body", "")).strip():
        return False, "approved, but the Email Body is empty."

    # 6. A reply means the conversation has moved on.
    reply = normalise(row.get("Reply Status"))
    if reply and reply not in {schema.REPLY_NONE, ""}:
        return False, (
            f"a reply was already received ({reply}) -- do not send the "
            "initial email again."
        )

    logger.debug("%s is approved and sendable.", outreach_id)
    return True, "approved"


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Count approval states across all outreach rows."""
    counts: dict[str, int] = {}
    sendable: list[dict[str, Any]] = []
    blocked: list[tuple[dict[str, Any], str]] = []
    invalid: list[tuple[dict[str, Any], str]] = []

    for row in rows:
        approval = normalise(row.get("Approval Status")) or "(blank)"
        counts[approval] = counts.get(approval, 0) + 1

        allowed, reason = is_sendable(row)
        if allowed:
            sendable.append(row)
        elif "not a valid value" in reason:
            invalid.append((row, reason))
        else:
            blocked.append((row, reason))

    return {
        "counts": counts,
        "sendable": sendable,
        "blocked": blocked,
        "invalid": invalid,
    }


def apply_decision(
    row: dict[str, Any], decision: str, edited_body: str | None = None
) -> dict[str, Any]:
    """Build the column updates for one approval decision.

    Email Status is kept consistent with Approval Status so the two
    columns cannot disagree and confuse a later run.
    """
    decision = normalise(decision)
    if decision not in schema.APPROVAL_STATUSES:
        raise ApprovalError(
            f"{decision!r} is not a valid decision. "
            f"Use one of: {', '.join(schema.APPROVAL_STATUSES)}."
        )

    updates: dict[str, Any] = {"Approval Status": decision}

    if decision == schema.APPROVAL_APPROVED:
        updates["Email Status"] = schema.EMAIL_APPROVED
        updates["Date Approved"] = _today()
    elif decision == schema.APPROVAL_REJECTED:
        # Keep the draft text -- you may want to see what was rejected.
        updates["Email Status"] = schema.EMAIL_NOT_STARTED
    elif decision == schema.APPROVAL_EDITED:
        # Edited is explicitly NOT approved; it returns to the queue.
        updates["Email Status"] = schema.EMAIL_DRAFTED
        if edited_body is not None:
            updates["Email Body"] = edited_body

    return updates


def _today() -> str:
    """Imported lazily to keep this module free of Excel dependencies."""
    from app.excel import today

    return today()

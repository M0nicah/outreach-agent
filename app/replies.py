"""Detecting and classifying replies to outreach emails.

Two separate jobs, deliberately kept apart:

  1. DETECTION -- Gmail search finds messages from the addresses we
     emailed. This is factual: either a message exists or it does not.
  2. CLASSIFICATION -- the AI reads the reply and says what it means.
     This is judgement, and it can be wrong.

Detection never depends on the AI, so a classification failure still
leaves you knowing that somebody replied. That ordering matters: missing
a reply entirely is far worse than mislabelling one.

A detected reply stops the follow-up sequence immediately, because
followups.has_replied() reads the same Reply Status column this module
writes.
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app import schema

logger = logging.getLogger(__name__)

# How far back to search. Outreach older than this is stale anyway.
DEFAULT_SEARCH_DAYS = 90

# Automated responses that are not real replies from a person.
AUTO_REPLY_MARKERS = [
    "out of office", "auto-reply", "autoreply", "automatic reply",
    "away from my desk", "on annual leave", "i am currently away",
    "do not reply to this", "this is an automated",
    "delivery status notification", "undeliverable", "mail delivery failed",
    "address not found", "returned to sender",
]


class ReplyError(Exception):
    """Raised when replies cannot be fetched."""


@dataclass
class FoundReply:
    """One message received from an address we contacted."""

    message_id: str
    thread_id: str
    from_address: str
    subject: str
    body: str
    received: str          # ISO date
    is_bounce: bool = False
    is_auto_reply: bool = False

    @property
    def needs_classification(self) -> bool:
        """Bounces and auto-replies are handled without spending an AI call."""
        return not (self.is_bounce or self.is_auto_reply)


# --- Reading Gmail messages ------------------------------------------------

def _decode_part(part: dict) -> str:
    """Decode one MIME part's text, if it has any."""
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_body(payload: dict) -> str:
    """Pull readable text out of a Gmail message payload.

    Prefers text/plain. Falls back to stripping tags from text/html,
    because plenty of corporate mail is HTML-only.
    """
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        return _decode_part(payload)

    if mime == "text/html":
        html = _decode_part(payload)
        return re.sub(r"<[^>]+>", " ", html)

    # Multipart: search the parts, preferring plain text.
    parts = payload.get("parts", [])
    plain = [p for p in parts if p.get("mimeType") == "text/plain"]
    for part in plain + parts:
        text = extract_body(part)
        if text.strip():
            return text

    return ""


def trim_quoted_text(body: str) -> str:
    """Remove the quoted original from a reply.

    A reply usually includes the whole email you sent. Feeding that back
    to the AI wastes tokens and confuses classification -- it might
    classify YOUR words rather than theirs.
    """
    markers = [
        r"\nOn .{0,80}wrote:",          # Gmail
        r"\n-{2,}\s*Original Message",   # Outlook
        r"\nFrom:.{0,120}\nSent:",       # Outlook headers
        r"\n_{10,}",                     # separator lines
        r"\n>{1,}\s",                    # quote markers
    ]
    earliest = len(body)
    for pattern in markers:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            earliest = min(earliest, match.start())

    return body[:earliest].strip()


def looks_automated(subject: str, body: str) -> tuple[bool, bool]:
    """Return (is_bounce, is_auto_reply) from the text itself."""
    combined = f"{subject}\n{body[:600]}".lower()

    bounce_markers = [
        "delivery status notification", "undeliverable",
        "mail delivery failed", "address not found", "returned to sender",
        "recipient rejected", "550 5.", "could not be delivered",
    ]
    if any(marker in combined for marker in bounce_markers):
        return True, False

    auto_markers = [
        "out of office", "auto-reply", "autoreply", "automatic reply",
        "away from my desk", "on annual leave", "i am currently away",
    ]
    if any(marker in combined for marker in auto_markers):
        return False, True

    return False, False


# --- Searching -------------------------------------------------------------

def build_search_query(addresses: list[str], days: int = DEFAULT_SEARCH_DAYS) -> str:
    """Build a Gmail search for messages from the people we contacted."""
    clean = [a for a in {a.strip().lower() for a in addresses} if a and a != schema.UNKNOWN.lower()]
    if not clean:
        return ""
    senders = " OR ".join(f"from:{a}" for a in clean)
    return f"({senders}) newer_than:{days}d -in:sent"


def fetch_replies(service, addresses: list[str], days: int = DEFAULT_SEARCH_DAYS) -> list[FoundReply]:
    """Find messages from the addresses we emailed.

    Detection is purely factual -- no AI involved. A failure here is
    raised rather than swallowed, because silently reporting "no replies"
    when the search broke would be misleading.
    """
    query = build_search_query(addresses, days)
    if not query:
        return []

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
    except Exception as exc:
        raise ReplyError(f"Gmail search failed: {exc}") from exc

    messages = result.get("messages", [])
    logger.info("Gmail search matched %d message(s).", len(messages))

    replies: list[FoundReply] = []
    for stub in messages:
        try:
            full = service.users().messages().get(
                userId="me", id=stub["id"], format="full"
            ).execute()
        except Exception as exc:
            logger.warning("Could not read message %s: %s", stub["id"], exc)
            continue

        payload = full.get("payload", {})
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", [])
        }

        from_header = headers.get("from", "")
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_header)
        from_address = match.group(0).lower() if match else from_header.lower()

        body = trim_quoted_text(extract_body(payload))
        subject = headers.get("subject", "")

        received = ""
        internal = full.get("internalDate")
        if internal:
            received = datetime.fromtimestamp(int(internal) / 1000).date().isoformat()

        is_bounce, is_auto = looks_automated(subject, body)

        replies.append(
            FoundReply(
                message_id=full.get("id", ""),
                thread_id=full.get("threadId", ""),
                from_address=from_address,
                subject=subject,
                body=body[:4000],
                received=received,
                is_bounce=is_bounce,
                is_auto_reply=is_auto,
            )
        )

    return replies


def match_to_outreach(
    replies: list[FoundReply],
    outreach: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
) -> list[tuple[FoundReply, dict[str, Any]]]:
    """Pair each reply with the outreach row it answers.

    Matches on the contact's email address. A reply we cannot match is
    reported rather than dropped -- it may be a forward from a colleague,
    which is useful to know about.
    """
    email_to_contact = {}
    for contact in contacts:
        email = str(contact.get("Email", "")).strip().lower()
        if email and email != schema.UNKNOWN.lower():
            email_to_contact[email] = str(contact.get("Contact ID", "")).strip()

    by_contact_id: dict[str, dict[str, Any]] = {}
    for row in outreach:
        if str(row.get("Email Status", "")).strip().upper() != schema.EMAIL_SENT:
            continue
        by_contact_id[str(row.get("Contact ID", "")).strip()] = row

    matched = []
    for reply in replies:
        contact_id = email_to_contact.get(reply.from_address)
        row = by_contact_id.get(contact_id) if contact_id else None
        if row:
            matched.append((reply, row))
        else:
            logger.info(
                "Reply from %s does not match any sent outreach -- read it by hand.",
                reply.from_address,
            )
    return matched


# --- Classification --------------------------------------------------------
#
# Everything below is judgement rather than fact, and is kept separate
# from detection on purpose. If classification fails, the reply is still
# recorded as received -- just as OTHER, for you to read yourself.

from pydantic import BaseModel, Field, ValidationError, field_validator  # noqa: E402

from app.ai import AIError  # noqa: E402
from app.config import PROJECT_ROOT, Config  # noqa: E402
from app.qualification import extract_json  # noqa: E402

CLASSIFIER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "reply_classifier.txt"


class ReplyClassification(BaseModel):
    """A validated reply classification."""

    classification: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""
    next_action: str = ""
    referred_to: str = schema.UNKNOWN
    deadline_mentioned: str = schema.UNKNOWN

    @field_validator("classification")
    @classmethod
    def _known(cls, value: str) -> str:
        value = (value or "").strip().upper()
        valid = set(schema.REPLY_STATUSES) - {schema.REPLY_NONE}
        if value not in valid:
            raise ValueError(f"must be one of {sorted(valid)}, got {value!r}")
        return value


def classify_reply(
    config: Config,
    reply: FoundReply,
    outreach_row: dict[str, Any],
    ai_caller,
    retries: int = 1,
) -> ReplyClassification:
    """Classify one reply.

    Bounces and auto-replies are handled without an AI call: their
    meaning is already known from the text, and paying to classify an
    out-of-office message would be wasteful.
    """
    if reply.is_bounce:
        return ReplyClassification(
            classification="NEGATIVE",
            confidence=1.0,
            summary="The message bounced -- the address does not accept mail.",
            next_action="Find a different contact address",
        )

    if reply.is_auto_reply:
        return ReplyClassification(
            classification="OTHER",
            confidence=1.0,
            summary="Automatic out-of-office reply, not an answer from a person.",
            next_action="Wait -- this was not a real reply",
        )

    if not CLASSIFIER_PROMPT_PATH.exists():
        raise ReplyError(f"Prompt file missing: {CLASSIFIER_PROMPT_PATH}")

    prompt = CLASSIFIER_PROMPT_PATH.read_text(encoding="utf-8").format(
        company_name=outreach_row.get("Company Name", schema.UNKNOWN),
        original_subject=outreach_row.get("Subject", ""),
        from_address=reply.from_address,
        reply_subject=reply.subject,
        reply_body=reply.body or "(the reply had no readable text)",
    )

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ai_caller(config, prompt)
            return ReplyClassification(**extract_json(raw))
        except (AIError, ValidationError, Exception) as exc:
            last_error = exc
            if attempt < retries:
                logger.warning("Reply classification failed (%s). Retrying.", exc)

    # Never lose the reply just because we could not label it.
    logger.error(
        "Could not classify the reply from %s (%s). Recording it as OTHER "
        "so it is not missed.", reply.from_address, last_error,
    )
    return ReplyClassification(
        classification="OTHER",
        confidence=0.0,
        summary=f"Reply received but could not be classified automatically: {last_error}",
        next_action="Read this reply yourself",
    )


def reply_updates(
    reply: FoundReply, result: ReplyClassification, existing_notes: str = ""
) -> dict[str, Any]:
    """Column updates recording a classified reply.

    Writing Reply Status is what stops the follow-up sequence:
    followups.has_replied() reads this same column.
    """
    notes = existing_notes.strip()
    extras = []
    if result.referred_to and result.referred_to != schema.UNKNOWN:
        extras.append(f"Referred to: {result.referred_to}")
    if result.deadline_mentioned and result.deadline_mentioned != schema.UNKNOWN:
        extras.append(f"Deadline: {result.deadline_mentioned}")
    if extras:
        notes = f"{notes} {' | '.join(extras)}".strip()

    updates = {
        "Reply Status": result.classification,
        "Reply Date": reply.received or date.today().isoformat(),
        "Reply Summary": result.summary[:500],
        "Next Action": result.next_action[:200],
    }
    if notes:
        updates["Notes"] = notes[:500]
    return updates

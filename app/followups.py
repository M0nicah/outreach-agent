"""Scheduling and drafting follow-up emails.

The schedule from the specification:

    Day 0      initial email
    Day 5      follow-up 1
    Day 12     follow-up 2
    Day 20-25  final follow-up

Two rules matter more than the dates:

1. STOP WHEN THEY REPLY. Any reply ends the sequence immediately. Chasing
   someone who has already answered is the fastest way to annoy an
   employer, and a rejection is still a reply.

2. STOP AFTER THE FINAL ONE. Three follow-ups is persistence; four is
   harassment. There is no code path that schedules a fourth.

This module works whether you send by hand or through Gmail, because it
reads Date Sent from the workbook rather than from any mail provider.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app import schema

logger = logging.getLogger(__name__)

# Days after the initial send. Configurable via .env in main.py.
DEFAULT_SCHEDULE = {
    1: 5,    # Follow-up 1
    2: 12,   # Follow-up 2
    3: 22,   # Final follow-up (the 20-25 window)
}

MAX_FOLLOWUPS = 3

# Reply statuses that end the sequence. NO_REPLY and a blank cell mean
# nothing has come back yet, so the sequence continues.
REPLIED_STATUSES = {"POSITIVE", "NEGATIVE", "REFERRAL", "CV_REQUEST", "INTERVIEW", "OTHER"}


class FollowUpError(Exception):
    """Raised when follow-up state cannot be determined."""


@dataclass
class FollowUpDue:
    """One follow-up that is due, or scheduled for the future."""

    outreach_row: dict[str, Any]
    number: int          # 1, 2 or 3
    due_date: date
    days_overdue: int    # 0 when due exactly today; negative when future

    @property
    def is_due(self) -> bool:
        return self.days_overdue >= 0

    @property
    def label(self) -> str:
        return "Final follow-up" if self.number == MAX_FOLLOWUPS else f"Follow-up {self.number}"


# --- Reading dates safely --------------------------------------------------

def parse_date(value: Any) -> date | None:
    """Read a date from a spreadsheet cell.

    Excel gives us strings or datetimes depending on how the cell was
    written, and a hand-typed cell may be neither. We return None rather
    than raising, so one bad cell cannot stop the whole report.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue

    logger.warning("Could not read date %r -- ignoring it.", value)
    return None


def has_replied(row: dict[str, Any]) -> bool:
    """True when a reply has been recorded, in any form."""
    status = str(row.get("Reply Status", "")).strip().upper()
    if status in REPLIED_STATUSES:
        return True
    # A reply date with no status still means they answered.
    return parse_date(row.get("Reply Date")) is not None


def followups_sent(row: dict[str, Any]) -> int:
    """How many follow-ups have already gone out for this row.

    The sheet has Follow Up 1 and Follow Up 2 columns. The final
    follow-up is recorded in Notes, because adding a third column for a
    once-per-thread event is not worth the schema churn.
    """
    count = 0
    for column in ["Follow Up 1", "Follow Up 2"]:
        if parse_date(row.get(column)):
            count += 1

    if "FOLLOWUP3:" in str(row.get("Notes", "")).upper():
        count += 1

    return count


# --- The schedule ----------------------------------------------------------

def next_followup(
    row: dict[str, Any],
    today: date | None = None,
    schedule: dict[int, int] | None = None,
) -> tuple[FollowUpDue | None, str]:
    """Work out the next follow-up for one outreach row.

    Returns (follow_up, reason). When follow_up is None, reason explains
    why the sequence is not continuing -- which is exactly what you want
    to read when auditing why someone was not chased.
    """
    today = today or date.today()
    schedule = schedule or DEFAULT_SCHEDULE

    # 1. Nothing to follow up if it was never sent.
    email_status = str(row.get("Email Status", "")).strip().upper()
    if email_status != schema.EMAIL_SENT:
        return None, "not sent yet"

    sent_on = parse_date(row.get("Date Sent"))
    if sent_on is None:
        return None, "sent, but Date Sent is empty or unreadable"

    # 2. THE STOP RULE. A reply ends the sequence, whatever it said.
    if has_replied(row):
        status = str(row.get("Reply Status", "")).strip().upper() or "a reply"
        return None, f"they replied ({status}) -- sequence stopped"

    # 3. Do not chase forever.
    already = followups_sent(row)
    if already >= MAX_FOLLOWUPS:
        return None, f"all {MAX_FOLLOWUPS} follow-ups sent -- sequence complete"

    number = already + 1
    due = sent_on + timedelta(days=schedule[number])

    return (
        FollowUpDue(
            outreach_row=row,
            number=number,
            due_date=due,
            days_overdue=(today - due).days,
        ),
        "scheduled",
    )


def find_due(
    outreach: list[dict[str, Any]],
    today: date | None = None,
    schedule: dict[int, int] | None = None,
    include_upcoming: bool = False,
) -> tuple[list[FollowUpDue], list[FollowUpDue], list[tuple[dict, str]]]:
    """Split all outreach rows into due, upcoming, and stopped.

    Returning the stopped rows with their reasons matters: you should be
    able to see WHY someone is not being chased, not just that they are
    absent from the list.
    """
    today = today or date.today()
    due: list[FollowUpDue] = []
    upcoming: list[FollowUpDue] = []
    stopped: list[tuple[dict, str]] = []

    for row in outreach:
        follow_up, reason = next_followup(row, today, schedule)
        if follow_up is None:
            # "not sent yet" is noise in a follow-up report.
            if reason != "not sent yet":
                stopped.append((row, reason))
            continue

        if follow_up.is_due:
            due.append(follow_up)
        elif include_upcoming:
            upcoming.append(follow_up)

    due.sort(key=lambda f: -f.days_overdue)
    upcoming.sort(key=lambda f: f.due_date)
    return due, upcoming, stopped


def record_followup_updates(number: int, today_str: str, notes: str = "") -> dict[str, Any]:
    """Column updates recording that a follow-up was sent."""
    if number == 1:
        return {"Follow Up 1": today_str}
    if number == 2:
        return {"Follow Up 2": today_str}
    if number == MAX_FOLLOWUPS:
        marker = f"FOLLOWUP3: sent {today_str}"
        return {"Notes": f"{notes} {marker}".strip() if notes else marker}
    raise FollowUpError(f"There is no follow-up number {number}.")


# --- Drafting follow-up emails ---------------------------------------------
#
# Each follow-up has a DIFFERENT job. If all three said "just checking
# in", they would read as automated nagging -- which is worse than not
# following up at all.

from pydantic import BaseModel, Field, ValidationError  # noqa: E402

from app.ai import AIError  # noqa: E402
from app.config import PROJECT_ROOT, Config  # noqa: E402
from app.email_drafts import check_quality, strip_trailing_name  # noqa: E402
from app.qualification import extract_json  # noqa: E402

FOLLOWUP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "followup.txt"

FOLLOWUP_PURPOSE = {
    1: (
        "Briefly check whether the earlier enquiry reached the right person, "
        "and offer to send a CV or short portfolio if that would help. Keep "
        "it to two or three sentences."
    ),
    2: (
        "Ask a narrower, easier question than the original: whether they take "
        "industrial attachment students at all, and if so when they usually "
        "recruit. An easy yes/no is more likely to get an answer than an "
        "open request."
    ),
    3: (
        "This is the final message. Say plainly that it is the last time you "
        "will write, thank them for their time, and leave the door open for "
        "them to get in touch later. Do not ask a new question -- a graceful "
        "close leaves a better impression than another request."
    ),
}

LENGTH_GUIDANCE = {
    1: "Aim for 60-90 words. Three short paragraphs at most.",
    2: "Aim for 50-80 words. Shorter than the first follow-up.",
    3: "Aim for 40-70 words. The shortest of the three.",
}


class FollowUpDraft(BaseModel):
    """A validated follow-up email."""

    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=40)


def build_followup_prompt(
    row: dict[str, Any], number: int, days_since: int, settings: dict[str, str]
) -> str:
    """Fill the follow-up template for one outreach row."""
    from app.email_drafts import format_student_profile

    if not FOLLOWUP_PROMPT_PATH.exists():
        raise FollowUpError(f"Prompt file missing: {FOLLOWUP_PROMPT_PATH}")

    return FOLLOWUP_PROMPT_PATH.read_text(encoding="utf-8").format(
        followup_number=number,
        days_since=days_since,
        length_guidance=LENGTH_GUIDANCE.get(number, "Keep it under 90 words."),
        followup_purpose=FOLLOWUP_PURPOSE.get(number, FOLLOWUP_PURPOSE[1]),
        original_subject=row.get("Subject", ""),
        original_body=row.get("Email Body", ""),
        student_profile=format_student_profile(settings),
    )


def generate_followup(
    config: Config,
    row: dict[str, Any],
    number: int,
    days_since: int,
    settings: dict[str, str],
    ai_caller,
    retries: int = 1,
) -> tuple[FollowUpDraft, list[str]]:
    """Draft one follow-up.

    Returns (draft, problems). The same quality checks as the initial
    email apply -- flattery and overclaiming are no more acceptable in a
    follow-up -- plus a check that it is not simply the original resent.
    """
    prompt = build_followup_prompt(row, number, days_since, settings)
    company = row.get("Company Name", "?")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ai_caller(config, prompt)
            draft = FollowUpDraft(**extract_json(raw))
        except (AIError, ValidationError, Exception) as exc:
            last_error = exc
            if attempt < retries:
                logger.warning("%s: follow-up attempt %d failed (%s). Retrying.",
                               company, attempt + 1, exc)
                continue
            raise FollowUpError(f"{company}: could not draft follow-up -- {exc}") from exc

        # The model often writes its own signature despite instructions.
        # Strip it here so the caller can append the real one cleanly.
        draft.body = strip_signature(draft.body, settings)

        # The initial-email checks apply, EXCEPT the minimum length: that
        # was written for a first email, and a short follow-up is correct
        # rather than suspicious. Brevity is the whole point of one.
        problems = [
            p for p in check_quality(draft.subject, draft.body)
            if "suspiciously short" not in p
        ]
        problems.extend(_followup_specific_problems(draft, row, settings))

        if problems and attempt < retries:
            logger.warning("%s: follow-up had %d problem(s), retrying once.",
                           company, len(problems))
            continue

        return draft, problems

    raise FollowUpError(f"{company}: {last_error}")


def strip_signature(body: str, settings: dict[str, str] | None = None) -> str:
    """Remove a trailing signature block from an email body.

    Needed in two places:

    1. Repeat detection -- the signature legitimately appears in BOTH the
       original and the follow-up, and flagging it as a repeated sentence
       is a false positive.
    2. Before appending our own signature -- models often produce one
       despite being told not to, and two signatures look careless.

    We cut at the student's name when it appears on a line of its own
    after the body, which is where a signature block starts.
    """
    if not settings:
        return body

    name = str(settings.get("Name", "")).strip()
    if not name:
        return body

    lines = body.split("\n")
    for index, line in enumerate(lines):
        # A line that is exactly the name, at least a little way in,
        # marks the start of the signature.
        if line.strip() == name and index > 0:
            return "\n".join(lines[:index]).rstrip()

    return body


def _followup_specific_problems(
    draft: FollowUpDraft,
    row: dict[str, Any],
    settings: dict[str, str] | None = None,
) -> list[str]:
    """Checks that only apply to follow-ups."""
    problems: list[str] = []

    word_count = len(draft.body.split())
    if word_count > 160:
        problems.append(
            f"too long for a follow-up ({word_count} words) -- it should be "
            "much shorter than the original"
        )

    # Catch a follow-up that is really just the original resent. Both
    # texts have their signature removed first: it appears in every
    # email by design, so matching on it is a false positive.
    original = strip_signature(str(row.get("Email Body", "")), settings)
    draft_body = strip_signature(draft.body, settings)
    original_sentences = [
        s.strip() for s in original.split(".") if len(s.strip().split()) >= 8
    ]
    reused = [s for s in original_sentences if s and s in draft_body]
    if reused:
        problems.append(
            f"repeats {len(reused)} sentence(s) from the original email "
            "instead of saying something new"
        )

    guilt_phrases = [
        "have not heard back", "haven't heard back", "no response",
        "you did not reply", "you have not replied", "i am still waiting",
        "as i mentioned before", "once again",
    ]
    lowered = draft.body.lower()
    for phrase in guilt_phrases:
        if phrase in lowered:
            problems.append(f"sounds like a complaint: '{phrase}'")

    return problems

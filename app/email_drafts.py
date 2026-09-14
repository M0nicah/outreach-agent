"""Generate outreach email drafts. Nothing here sends anything.

Every draft is written to the Outreach sheet with:
    Approval Status = PENDING
    Email Status    = DRAFTED

That is the human gate from your specification. Stage 8's sender will
refuse to touch anything that is not APPROVED.

The AI writes the email, but Python checks it afterwards. Two failure
modes matter enough to verify in code rather than trust to a prompt:

  1. Flattery      -- "I have long admired your innovative company"
  2. Overclaiming  -- making a student sound like a senior engineer

Both are things language models do by default, and both would embarrass
the student in front of a real employer. A draft that fails these checks
is still saved, but flagged in the Notes column so it is reviewed before
approval rather than silently dropped.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app import schema
from app.ai import AIError
from app.campaigns import campaign_focus, choose_campaign
from app.config import PROJECT_ROOT, Config
from app.qualification import extract_json

logger = logging.getLogger(__name__)

PROMPT_PATH = PROJECT_ROOT / "prompts" / "personalization.txt"


class DraftError(Exception):
    """Raised when a draft cannot be produced."""


# --- Quality checks --------------------------------------------------------

# Flattery and marketing language. Catching these in code means the check
# survives a prompt edit or a model change.
BANNED_PHRASES = [
    "i have always admired", "i have long admired", "i admire",
    "your innovative", "innovative company", "industry-leading",
    "industry leading", "world-class", "cutting-edge", "cutting edge",
    "i am impressed", "impressed by your", "renowned", "prestigious",
    "esteemed", "i am passionate", "passionate about", "thrilled",
    "delighted", "leverage", "synergy", "i hope this email finds you well",
    "your esteemed organisation", "your esteemed organization",
]

# Details about the student that the profile does not contain, so any
# mention of them is invention. The year-of-study case is real: a draft
# described the student as a "second-year student" when Settings only
# records an expected graduation year.
FABRICATED_DETAIL_PHRASES = [
    "first-year", "first year student", "second-year", "second year student",
    "third-year", "third year student", "fourth-year", "final-year",
    "final year student", "freshman", "sophomore", "penultimate year",
]

# Claims a student cannot honestly make.
OVERCLAIM_PHRASES = [
    "experienced data scientist", "experienced engineer",
    "years of experience", "extensive experience", "proven track record",
    "track record of", "seasoned", "expert in", "i am an expert",
    "accomplished", "specialise in", "specialize in",
    "deep expertise", "strong expertise", "professional experience in",
]


def check_quality(subject: str, body: str) -> list[str]:
    """Return a list of problems found in a draft. Empty means it passed."""
    problems: list[str] = []
    combined = f"{subject}\n{body}".lower()

    for phrase in BANNED_PHRASES:
        if phrase in combined:
            problems.append(f"flattery/marketing language: '{phrase}'")

    for phrase in OVERCLAIM_PHRASES:
        if phrase in combined:
            problems.append(f"overstates the student's experience: '{phrase}'")

    for phrase in FABRICATED_DETAIL_PHRASES:
        if phrase in combined:
            problems.append(
                f"invents a detail not in the profile: '{phrase}' "
                "(Settings records a graduation year, not a year of study)"
            )

    if "!" in body:
        problems.append("contains an exclamation mark")

    word_count = len(body.split())
    if word_count > 260:
        problems.append(f"too long ({word_count} words)")
    elif word_count < 60:
        problems.append(f"suspiciously short ({word_count} words)")

    # A draft addressed to a specific person means a name was invented,
    # because we never pass one in.
    lowered = body.lower()
    if lowered.startswith("dear ") :
        greeting = body.split("\n")[0].lower()
        if not any(
            word in greeting
            for word in ["team", "hiring", "recruitment", "sir", "madam", "hr"]
        ):
            problems.append(f"appears to address a named person: {body.split(chr(10))[0]!r}")

    return problems


# --- Validated AI output ---------------------------------------------------

class Draft(BaseModel):
    """One email draft, validated."""

    subject: str = Field(min_length=5, max_length=200)
    body: str = Field(min_length=100)
    grounded_claims: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# --- Building the email ----------------------------------------------------

def strip_trailing_name(body: str, name: str) -> str:
    """Remove a sign-off name so the signature does not repeat it.

    The prompt asks the model not to sign the email, because the
    signature block is appended from Settings. Models do it anyway, so
    we strip a trailing name defensively rather than shipping an email
    that says the student's name twice in a row.
    """
    if not name:
        return body

    lines = body.rstrip().split("\n")
    # Only look at the last two lines: a name on its own, possibly after
    # a closing like "Sincerely,".
    while lines and lines[-1].strip().lower() == name.strip().lower():
        lines.pop()
    return "\n".join(lines).rstrip()


def build_signature(settings: dict[str, str]) -> str:
    """Build the signature from the Settings sheet.

    Only fields that are actually filled in appear. An unfilled portfolio
    is omitted rather than shown as FILL_IN or invented.
    """
    lines = [str(settings.get("Name", "")).strip()]

    degree = str(settings.get("Degree", "")).strip()
    university = str(settings.get("University", "")).strip()
    graduation = str(settings.get("Graduation", "")).strip()
    if degree and university:
        study = f"{degree}, {university}"
        if graduation and graduation != "FILL_IN":
            study += f" (expected {graduation})"
        lines.append(study)

    for label, key in [
        ("", "Email"),
        ("LinkedIn: ", "LinkedIn"),
        ("GitHub: ", "GitHub"),
        ("Portfolio: ", "Portfolio"),
    ]:
        value = str(settings.get(key, "")).strip()
        if value and value != "FILL_IN":
            lines.append(f"{label}{value}")

    return "\n".join(line for line in lines if line)


def format_student_profile(settings: dict[str, str]) -> str:
    """Describe the student for the prompt, skipping unfilled fields."""
    wanted = [
        "Name", "University", "Degree", "Graduation", "Skills",
        "Target Roles", "Internship Type", "Availability",
        "Preferred Locations", "Experience Level",
    ]
    lines = []
    for key in wanted:
        value = str(settings.get(key, "")).strip()
        if value and value != "FILL_IN":
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def build_prompt(
    company: dict[str, Any], campaign: str, settings: dict[str, str]
) -> str:
    """Fill the personalisation template for one company."""
    if not PROMPT_PATH.exists():
        raise DraftError(f"Prompt file missing: {PROMPT_PATH}")

    return PROMPT_PATH.read_text(encoding="utf-8").format(
        student_profile=format_student_profile(settings),
        company_name=company.get("Company Name", schema.UNKNOWN),
        what_they_do=company.get("What They Do") or schema.UNKNOWN,
        technology_function=company.get("Technology Function") or schema.UNKNOWN,
        internship_evidence=company.get("Internship Evidence") or schema.UNKNOWN,
        campaign_focus=campaign_focus(campaign),
    )


def generate_draft(
    config: Config,
    company: dict[str, Any],
    settings: dict[str, str],
    ai_caller,
    retries: int = 1,
) -> tuple[Draft, str, list[str]]:
    """Generate one draft.

    Returns (draft, campaign, quality_problems). Problems are returned
    rather than raised: a flawed draft is still useful to a human
    reviewer, and hiding it would be worse than flagging it.
    """
    campaign = choose_campaign(company)
    prompt = build_prompt(company, campaign, settings)
    name = company.get("Company Name", "?")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ai_caller(config, prompt)
            draft = Draft(**extract_json(raw))
        except (AIError, ValidationError, Exception) as exc:
            last_error = exc
            if attempt < retries:
                logger.warning("%s: draft attempt %d failed (%s). Retrying.",
                               name, attempt + 1, exc)
                continue
            raise DraftError(f"{name}: could not generate a draft -- {exc}") from exc

        problems = check_quality(draft.subject, draft.body)
        if problems and attempt < retries:
            # Worth one retry: the model often writes a cleaner second
            # draft when the first drifts into marketing language.
            logger.warning(
                "%s: draft had %d quality problem(s), retrying once.",
                name, len(problems),
            )
            continue

        return draft, campaign, problems

    raise DraftError(f"{name}: {last_error}")


def to_outreach_row(
    outreach_id: str,
    company: dict[str, Any],
    contact: dict[str, Any],
    draft: Draft,
    campaign: str,
    problems: list[str],
    today: str,
) -> dict[str, Any]:
    """Build the Outreach-sheet row for one draft.

    Approval Status is always PENDING. There is no code path that
    creates an approved row.
    """
    notes = ""
    if problems:
        notes = "REVIEW CAREFULLY -- " + "; ".join(problems)

    return {
        "Outreach ID": outreach_id,
        "Company ID": company.get("Company ID", schema.UNKNOWN),
        "Contact ID": contact.get("Contact ID", schema.UNKNOWN),
        "Company Name": company.get("Company Name", schema.UNKNOWN),
        "Contact Name": contact.get("Contact Name", schema.UNKNOWN),
        "Campaign": campaign,
        "Subject": draft.subject,
        "Email Body": draft.body,
        "Approval Status": schema.APPROVAL_PENDING,
        "Email Status": schema.EMAIL_DRAFTED,
        "Date Drafted": today,
        "Reply Status": schema.REPLY_NONE,
        "Notes": notes,
    }


def existing_outreach_keys(outreach: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """(company, contact, campaign) triples already drafted.

    Duplicate prevention, per your specification: the same contact must
    not receive two initial emails for the same campaign.
    """
    return {
        (
            str(row.get("Company ID", "")).strip(),
            str(row.get("Contact ID", "")).strip(),
            str(row.get("Campaign", "")).strip(),
        )
        for row in outreach
    }

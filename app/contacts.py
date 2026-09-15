"""Find real contact routes into a company.

THE RULE THIS MODULE EXISTS TO ENFORCE: never invent a person.

Most "AI contact finder" tools hallucinate plausible addresses like
recruitment@company.com, and a plausible address is worse than none --
it bounces, or it reaches a stranger. So the responsibilities here are
deliberately split:

    Python  finds   -- regex over text we actually fetched
    AI      labels  -- classifies only what Python already found

The AI is never asked to produce an email address or a person's name. It
only reads observed text and answers "what kind of contact is this?".

What counts as a contact here is broader than "a person". A careers
portal is a legitimate, honest route into a company, and for large
organisations it is usually the RIGHT route. We record it as such rather
than pretending we found a recruiter's inbox.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from app import schema
from app.research import EvidencePack, fetch_page

logger = logging.getLogger(__name__)

# Deliberately strict: we would rather miss an address than invent one.
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Addresses that are never a useful outreach target.
JUNK_EMAIL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "mailer-daemon", "abuse", "spam", "unsubscribe", "bounce",
)

# File extensions that regex mistakes for email domains.
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

# Country names that appear in address local parts at multinationals.
# An address like botswanahr@ or hr.uganda@ is a real HR inbox, but for
# the WRONG country -- emailing it about a Nairobi placement wastes
# everyone's time. We demote these rather than dropping them, because
# occasionally the regional office is the only route in.
OTHER_COUNTRY_HINTS = (
    "botswana", "uganda", "tanzania", "zambia", "zimbabwe", "zim",
    "rwanda", "malawi", "nigeria", "ghana", "southafrica", "sa",
    "mozambique", "drc", "congo", "burundi", "ethiopia", "egypt",
    "uk", "usa", "india",
)


def wrong_country(email: str, home_country: str = "kenya") -> bool:
    """True when the address names a country other than the home one."""
    local = email.split("@")[0].lower()
    if home_country.lower() in local:
        return False
    return any(
        hint in local
        for hint in OTHER_COUNTRY_HINTS
        if hint not in home_country.lower()
    )


# Local parts that suggest a recruitment-related inbox. Used only to RANK
# addresses we actually found -- never to construct one.
RECRUITMENT_HINTS = (
    "career", "recruit", "hr", "jobs", "job", "talent", "internship",
    "intern", "graduate", "hiring", "people", "attachment",
)

# Local parts that are never a sensible target for a job application,
# even though they are real published addresses.
#
# Two real examples from live runs: raising.concerns@ is a whistleblowing
# line, and abeer.etefa@wfp.org is a named press officer. Writing to
# either about an internship would be unwelcome and ineffective.
UNSUITABLE_HINTS = (
    "raising.concerns", "whistle", "compliance", "fraud", "abuse",
    "press", "media", "privacy", "legal", "procurement", "tender",
    "billing", "invoice", "complaints", "safeguarding",
)


def is_unsuitable_inbox(email: str) -> bool:
    """True when the address exists but is the wrong place to apply."""
    local = email.split("@")[0].lower()
    return any(hint in local for hint in UNSUITABLE_HINTS)

# Pages worth checking for contact details, beyond what research.py
# fetched. Ordered by how likely they are to carry a real address.
#
# Widened after finding that two pages (homepage + careers) missed
# addresses that were published elsewhere on the same site -- Amref's
# info@ address, for example.
CONTACT_PATHS = ["/contact", "/contact-us", "/contacts", "/about-us", "/about"]

# How many contact pages to try per company. Each is an HTTP request
# against a possibly-slow site, so this trades run time against coverage.
# Three is the point where the extra pages stopped finding much in
# testing, while five made a 77-company run unacceptably slow.
MAX_CONTACT_PAGES = 3


@dataclass
class FoundContact:
    """One real, observed contact route.

    Every field is either something we saw, or the literal string
    UNKNOWN. There is no third option.
    """

    email: str = schema.UNKNOWN
    contact_name: str = schema.UNKNOWN
    contact_role: str = schema.UNKNOWN
    contact_type: str = schema.UNKNOWN
    linkedin: str = schema.UNKNOWN
    source_url: str = schema.UNKNOWN
    why: str = ""
    # How the address was obtained. "observed" is the only acceptable
    # value for an email -- anything else would mean we made it up.
    method: str = "observed"

    def to_row(self, contact_id: str, company: dict[str, Any]) -> dict[str, Any]:
        """Render as a Contacts-sheet row."""
        return {
            "Contact ID": contact_id,
            "Company ID": company.get("Company ID", schema.UNKNOWN),
            "Company Name": company.get("Company Name", schema.UNKNOWN),
            "Contact Name": self.contact_name,
            "Contact Role": self.contact_role,
            "Contact Type": self.contact_type,
            "Email": self.email,
            # Observed on the company's own site, but not SMTP-verified.
            "Email Verified": "OBSERVED" if self.email != schema.UNKNOWN else "NO",
            "LinkedIn": self.linkedin,
            "Source": self.source_url,
            "Why This Contact": self.why,
            "Contact Status": "NEW",
        }


@dataclass
class ContactSearch:
    """Everything found for one company."""

    company_name: str
    contacts: list[FoundContact] = field(default_factory=list)
    careers_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def found_anything(self) -> bool:
        return bool(self.contacts)


# --- Email extraction ------------------------------------------------------

def is_usable_email(email: str, company_domain: str = "") -> bool:
    """Reject addresses that are junk, or belong to someone else.

    The domain check matters: pages often embed third-party addresses
    (agencies, CMS vendors). Emailing those would be both useless and
    rude.
    """
    email = email.lower().strip()

    local, _, domain = email.partition("@")
    if not local or not domain:
        return False
    if local.startswith(JUNK_EMAIL_PREFIXES):
        return False
    if email.endswith(JUNK_EMAIL_SUFFIXES):
        return False
    # Regex sometimes captures things like "2x@2x.png" from image markup.
    if any(char.isdigit() for char in domain.split(".")[0]) and len(domain) < 6:
        return False

    if company_domain:
        # Accept the company's domain, a subdomain of it, OR its parent.
        #
        # The parent case matters: a site at ilabafrica.strathmore.edu
        # publishes ilabafrica@strathmore.edu. Requiring an exact or
        # child match silently dropped that perfectly valid address, so
        # we compare the registrable root instead.
        root = company_domain.lower().removeprefix("www.")
        if not (
            domain == root
            or domain.endswith("." + root)
            or root.endswith("." + domain)
        ):
            return False

    return True


def extract_emails(text: str, company_domain: str = "") -> list[str]:
    """Pull usable email addresses out of already-fetched page text."""
    found = {
        email.lower()
        for email in EMAIL_PATTERN.findall(text)
        if is_usable_email(email, company_domain)
    }
    return sorted(found)


def rank_emails(emails: list[str]) -> list[str]:
    """Put recruitment-ish addresses first.

    This only reorders addresses we actually observed. It never creates
    one, and an unranked general address is still a real address.
    """

    def score(email: str) -> tuple[int, int, str]:
        local = email.split("@")[0].lower()
        # Unsuitable inboxes (whistleblowing, press, legal) go last of all.
        if is_unsuitable_inbox(email):
            return (2, len(RECRUITMENT_HINTS), email)

        # Then an address for another country, however recruitment-ish.
        country_penalty = 1 if wrong_country(email) else 0

        for index, hint in enumerate(RECRUITMENT_HINTS):
            if hint in local:
                return (country_penalty, index, email)
        return (country_penalty, len(RECRUITMENT_HINTS), email)

    return sorted(emails, key=score)


def domain_of(url: str) -> str:
    """The registrable host of a URL, without 'www.'."""
    if not url or url == schema.UNKNOWN:
        return ""
    return urlparse(url).netloc.lower().removeprefix("www.")


# --- Searching -------------------------------------------------------------

def find_contacts(company: dict[str, Any], pack: EvidencePack) -> ContactSearch:
    """Find contact routes for one company, using only observed text.

    Reuses the pages research.py already fetched, then tries a couple of
    contact pages. No AI is involved at this point.
    """
    name = str(company.get("Company Name", "?"))
    search = ContactSearch(company_name=name)

    website = str(company.get("Website", "")).strip()
    if not website or website == schema.UNKNOWN:
        search.notes.append("No website on file, so no contact page could be checked.")
        return search

    company_domain = domain_of(website)

    # Start from the pages we already have -- no extra requests needed.
    pages = list(pack.pages)

    # Then try contact pages, which is where addresses usually live.
    if pack.pages:
        base = f"{urlparse(pack.pages[0].url).scheme}://{urlparse(pack.pages[0].url).netloc}"
        checked = 0
        for path in CONTACT_PATHS:
            if checked >= MAX_CONTACT_PAGES:
                break
            page, error = fetch_page(base + path)
            checked += 1
            if page:
                pages.append(page)
                logger.info("%s: read %s", name, page.url)
                # Stop once we have actually found an address, rather than
                # after the first page that merely loads.
                if extract_emails(page.text, company_domain):
                    break
            elif error:
                logger.debug("%s: %s", name, error)

    if not pages:
        search.notes.append(
            "No pages could be retrieved, so no contact details were observed."
        )
        return search

    # Collect addresses, remembering which page each came from.
    seen: dict[str, str] = {}
    for page in pages:
        for email in extract_emails(page.text, company_domain):
            seen.setdefault(email, page.url)

    for email in rank_emails(list(seen)):
        note = f"Email address published on {seen[email]}"
        if wrong_country(email):
            note = (
                f"{note}. WARNING: this address names another country -- "
                "check whether it is the right office before writing."
            )
        search.contacts.append(
            FoundContact(email=email, source_url=seen[email], why=note)
        )

    # A careers portal is a legitimate route in, and for a large company
    # it is often the correct one. Record it honestly as an application
    # route rather than pretending it is a person.
    if pack.careers_url:
        search.careers_url = pack.careers_url
        search.contacts.append(
            FoundContact(
                email=schema.UNKNOWN,
                contact_type="Internship/Graduate Recruitment",
                contact_role="Careers / application portal",
                source_url=pack.careers_url,
                why=(
                    f"Careers page published at {pack.careers_url}. "
                    "Apply through this portal -- no individual contact was observed."
                ),
            )
        )

    if not search.contacts:
        search.notes.append(
            "Pages were read but no email address or careers page was published on them."
        )

    return search


# --- Turning findings into rows -------------------------------------------

def build_contact_rows(
    search: ContactSearch, company: dict[str, Any], start_id: int, limit: int = 3
) -> list[dict[str, Any]]:
    """Build Contacts-sheet rows, capped so one company cannot flood the sheet."""
    rows = []
    for offset, contact in enumerate(search.contacts[:limit]):
        rows.append(contact.to_row(f"P{start_id + offset:03d}", company))
    return rows


def existing_contact_keys(contacts: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """(company id, email) pairs already in the sheet, for duplicate prevention."""
    keys = set()
    for contact in contacts:
        company_id = str(contact.get("Company ID", "")).strip()
        email = str(contact.get("Email", "")).strip().lower()
        source = str(contact.get("Source", "")).strip().lower()
        # Key on the email, or on the source URL when there is no email,
        # so a careers portal is not added twice either.
        keys.add((company_id, email if email and email != schema.UNKNOWN.lower() else source))
    return keys


# --- AI classification -----------------------------------------------------
#
# The AI's only job here is to LABEL contacts Python already found. The
# guard below enforces that: any address the model returns which we did
# not actually observe is thrown away and logged. That makes fabrication
# structurally impossible rather than merely discouraged.

from pydantic import BaseModel, Field, ValidationError, field_validator  # noqa: E402

from app.ai import AIError  # noqa: E402
from app.config import PROJECT_ROOT, Config  # noqa: E402
from app.qualification import extract_json  # noqa: E402

CONTACT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "contacts.txt"

SUITABILITY_ORDER = {"GOOD": 0, "ACCEPTABLE": 1, "POOR": 2, schema.UNKNOWN: 3}


class ContactLabel(BaseModel):
    """One classified contact, validated."""

    email: str = schema.UNKNOWN
    contact_type: str = schema.UNKNOWN
    suitability: str = schema.UNKNOWN
    why: str = ""

    @field_validator("contact_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        value = (value or "").strip()
        if value in schema.CONTACT_TYPES:
            return value
        # An unrecognised label is downgraded rather than rejected -- the
        # classification is advisory, and a wrong label must not throw
        # away a real contact.
        return "Other"

    @field_validator("suitability")
    @classmethod
    def _known_suitability(cls, value: str) -> str:
        value = (value or "").strip().upper()
        return value if value in {"GOOD", "ACCEPTABLE", "POOR"} else schema.UNKNOWN


class ContactLabels(BaseModel):
    contacts: list[ContactLabel] = Field(default_factory=list)


def format_observed(search: ContactSearch) -> str:
    """Render the found contacts as the list shown to the AI."""
    lines = []
    for index, contact in enumerate(search.contacts, start=1):
        if contact.email != schema.UNKNOWN:
            lines.append(
                f"{index}. EMAIL: {contact.email}\n"
                f"   Published on: {contact.source_url}"
            )
        else:
            lines.append(
                f"{index}. CAREERS PORTAL (no email address)\n"
                f"   URL: {contact.source_url}"
            )
    return "\n".join(lines) if lines else "(none found)"


def classify_contacts(
    config: Config,
    company: dict[str, Any],
    search: ContactSearch,
    ai_caller,
) -> ContactSearch:
    """Ask the AI to label the observed contacts, then enforce honesty.

    Any email in the AI's reply that we did not observe is discarded. The
    model cannot add a contact, only describe one.
    """
    if not search.found_anything:
        return search

    template = CONTACT_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        company_name=company.get("Company Name", schema.UNKNOWN),
        what_they_do=company.get("What They Do") or schema.UNKNOWN,
        observed=format_observed(search),
    )

    try:
        raw = ai_caller(config, prompt)
        labels = ContactLabels(**extract_json(raw))
    except (AIError, ValidationError, Exception) as exc:
        # Classification is a nice-to-have. If it fails we keep the real
        # contacts with UNKNOWN labels rather than losing them.
        logger.warning(
            "%s: could not classify contacts (%s). Keeping them unlabelled.",
            search.company_name, exc,
        )
        return search

    observed_emails = {c.email.lower() for c in search.contacts}
    by_email = {c.email.lower(): c for c in search.contacts}

    for label in labels.contacts:
        key = (label.email or schema.UNKNOWN).lower()

        if key not in observed_emails:
            # The model produced an address we never saw. This is the
            # fabrication case the whole design exists to prevent.
            logger.error(
                "%s: AI returned an email we did not observe (%r). Discarded.",
                search.company_name, label.email,
            )
            continue

        contact = by_email[key]
        if label.contact_type != schema.UNKNOWN:
            contact.contact_type = label.contact_type
        if label.why:
            contact.why = f"{label.why} (source: {contact.source_url})"
        contact.contact_role = contact.contact_role or schema.UNKNOWN
        # Stash suitability for ranking; it is not a spreadsheet column.
        contact.method = label.suitability or schema.UNKNOWN

    # Best routes first, so the top contact is the one to email.
    search.contacts.sort(key=lambda c: SUITABILITY_ORDER.get(c.method, 3))
    return search

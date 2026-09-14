"""A fake AI caller for free, offline, deterministic testing.

This is NOT a second qualification engine and its judgement means nothing.
It exists so we can exercise the plumbing -- prompt building, JSON
parsing, validation, Excel writing, error paths -- without paying for or
waiting on API calls.

It works by crude keyword matching on the company name and evidence. The
real decisions come from the real model; never read a mock result as an
opinion about a company.
"""

import json
import logging
import re

from app import schema
from app.config import Config

logger = logging.getLogger(__name__)

# Names that clearly state a business type we reject.
REJECT_WORDS = [
    "cyber cafe", "cyber café", "internet cafe", "computer repair",
    "phone repair", "accessories", "printing", "stationery", "mpesa",
    "m-pesa",
]

# Evidence text suggesting a genuine technology operation.
QUALIFY_WORDS = [
    "engineering", "software", "developer", "data", "internship",
    "graduate programme", "graduate program", "technology", "platform",
    "api", "cloud",
]


def _scored(
    classification: str,
    scores: tuple[int, int, int, int, int, int],
    reason: str,
    evidence: list[str],
    roles: list[str],
    contact_type: str,
    what: str,
    tech: str,
    internship: str,
) -> str:
    """Assemble a mock reply in exactly the shape the real model returns."""
    tech_rel, intern, skills, maturity, contact, geo = scores
    total = sum(scores)

    if classification == schema.RESEARCH_REJECT:
        priority = "REJECT"
    elif total >= 90:
        priority = "A"
    elif total >= 75:
        priority = "B"
    elif total >= 60:
        priority = "C"
    elif total >= 40:
        priority = "D"
    else:
        priority = "REJECT"

    return json.dumps(
        {
            "classification": classification,
            "confidence": 0.8,
            "technology_relevance": tech_rel,
            "internship_likelihood": intern,
            "skills_match": skills,
            "maturity": maturity,
            "contactability": contact,
            "geographic": geo,
            "total_score": total,
            "priority": priority,
            "reason": f"[MOCK RESULT -- not a real assessment] {reason}",
            "evidence": evidence,
            "relevant_roles": roles,
            "suggested_contact_type": contact_type,
            "what_they_do": what,
            "technology_function": tech,
            "internship_evidence": internship,
            "careers_url": schema.UNKNOWN,
        }
    )


def mock_call(config: Config, prompt: str) -> str:
    """Stand-in for call_openai with the same signature."""
    lowered = prompt.lower()

    # Pull the company name back out of the filled-in prompt.
    match = re.search(r"^Name: (.+)$", prompt, re.MULTILINE)
    name = match.group(1).strip() if match else "Unknown"
    name_lower = name.lower()

    logger.debug("Mock AI answering for %s", name)

    if any(word in name_lower for word in REJECT_WORDS):
        return _scored(
            schema.RESEARCH_REJECT,
            (2, 0, 1, 1, 2, 8),
            f"The name '{name}' states the business type directly, and that "
            "type offers no professional technology or data function.",
            [f"VERIFIED: company name is '{name}'"],
            [],
            "Other",
            schema.UNKNOWN,
            schema.UNKNOWN,
            schema.UNKNOWN,
        )

    if "no evidence retrieved" in lowered:
        return _scored(
            schema.RESEARCH_NEEDS_REVIEW,
            (12, 5, 10, 5, 3, 9),
            "No evidence could be retrieved, so no confident judgement is "
            "possible. This is a data gap, not a negative finding.",
            ["VERIFIED: no pages were retrieved for this organisation"],
            [],
            schema.UNKNOWN,
            schema.UNKNOWN,
            schema.UNKNOWN,
            schema.UNKNOWN,
        )

    hits = sum(1 for word in QUALIFY_WORDS if word in lowered)
    if hits >= 4:
        return _scored(
            schema.RESEARCH_QUALIFY,
            (22, 18, 17, 9, 8, 9),
            f"Retrieved pages mention technology and hiring signals "
            f"({hits} indicators matched).",
            ["VERIFIED: retrieved pages reference technology work"],
            ["Data Analyst Intern", "Software Intern"],
            "Internship/Graduate Recruitment",
            "Technology organisation (mock summary).",
            "Technology and data teams (mock summary).",
            "Mock evidence -- not a real finding.",
        )

    return _scored(
        schema.RESEARCH_NEEDS_REVIEW,
        (14, 6, 11, 6, 6, 9),
        f"Evidence was retrieved but only {hits} technology indicators were "
        "found, which is not enough for a confident decision.",
        ["VERIFIED: pages retrieved but technology signals were weak"],
        [],
        schema.UNKNOWN,
        schema.UNKNOWN,
        schema.UNKNOWN,
        schema.UNKNOWN,
    )

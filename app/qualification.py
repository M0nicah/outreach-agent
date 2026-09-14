"""Send evidence to the AI and validate the structured result.

The flow for one company:

    evidence (from research.py)
        -> prompt
        -> AI
        -> raw JSON text
        -> pydantic validation   <-- the gate
        -> dict ready for Excel

Pydantic is the important part. An LLM will occasionally return a score of
30 out of 25, a misspelled classification, or prose wrapped around the
JSON. None of that should ever reach your spreadsheet. If validation
fails we retry once, and if it fails again the company is marked ERROR
for you to look at -- never silently skipped.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app import schema
from app.ai import AIError, get_ai_caller  # noqa: F401 (re-exported for callers)
from app.config import Config, PROJECT_ROOT
from app.research import EvidencePack

logger = logging.getLogger(__name__)

PROMPT_PATH = PROJECT_ROOT / "prompts" / "qualification.txt"

# Score ceilings, straight from the specification.
MAX_SCORES = {
    "technology_relevance": 25,
    "internship_likelihood": 25,
    "skills_match": 20,
    "maturity": 10,
    "contactability": 10,
    "geographic": 10,
}


class QualificationError(Exception):
    """Raised when the AI result cannot be obtained or trusted."""


def priority_for_score(score: int) -> str:
    """Map a total score to its priority band.

    Kept in Python because it is simple arithmetic and must be consistent.
    90-100 = A, 75-89 = B, 60-74 = C, 40-59 = D, below 40 = REJECT.
    """
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "REJECT"


# --- The validated shape of an AI answer -----------------------------------

class Qualification(BaseModel):
    """One company's AI assessment, validated.

    Anything that does not fit this shape is rejected before it can reach
    the workbook.
    """

    classification: str
    confidence: float = Field(ge=0.0, le=1.0)

    technology_relevance: int = Field(ge=0, le=25)
    internship_likelihood: int = Field(ge=0, le=25)
    skills_match: int = Field(ge=0, le=20)
    maturity: int = Field(ge=0, le=10)
    contactability: int = Field(ge=0, le=10)
    geographic: int = Field(ge=0, le=10)
    total_score: int = Field(ge=0, le=100)

    priority: str
    reason: str
    evidence: list[str] = Field(default_factory=list)
    relevant_roles: list[str] = Field(default_factory=list)
    suggested_contact_type: str = schema.UNKNOWN
    what_they_do: str = schema.UNKNOWN
    technology_function: str = schema.UNKNOWN
    internship_evidence: str = schema.UNKNOWN
    careers_url: str = schema.UNKNOWN

    @field_validator("classification")
    @classmethod
    def _known_classification(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = {
            schema.RESEARCH_QUALIFY,
            schema.RESEARCH_NEEDS_REVIEW,
            schema.RESEARCH_REJECT,
        }
        if value not in allowed:
            raise ValueError(f"must be one of {sorted(allowed)}, got {value!r}")
        return value

    @field_validator("priority")
    @classmethod
    def _known_priority(cls, value: str) -> str:
        value = value.strip().upper()
        allowed = schema.PRIORITIES + [schema.RESEARCH_NEEDS_REVIEW]
        if value not in allowed:
            raise ValueError(f"must be one of {allowed}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _priority_matches_classification(self) -> "Qualification":
        """Derive priority in Python rather than trusting the AI's band.

        Two problems this fixes:

        1. Band arithmetic is deterministic, so there is no reason to let
           a language model do it.
        2. The AI was labelling NEEDS_REVIEW companies as priority
           "REJECT" simply because a no-evidence company scores low. That
           is wrong and dangerous: it makes a company we could not
           research look like one we assessed and turned down.

        NEEDS_REVIEW therefore always gets the literal priority
        "NEEDS_REVIEW" -- it is a queue for you to look at, not a ranking.
        """
        if self.classification == schema.RESEARCH_REJECT:
            correct = "REJECT"
        elif self.classification == schema.RESEARCH_NEEDS_REVIEW:
            correct = schema.RESEARCH_NEEDS_REVIEW
        else:
            correct = priority_for_score(self.total_score)

        if correct != self.priority:
            logger.debug(
                "Priority corrected from %r to %r for a %s result.",
                self.priority, correct, self.classification,
            )
            object.__setattr__(self, "priority", correct)
        return self

    @model_validator(mode="after")
    def _totals_add_up(self) -> "Qualification":
        """Recompute the total rather than trusting the model's arithmetic.

        LLMs are unreliable at addition. We log the discrepancy (it is a
        useful signal that a prompt is drifting) and use the correct sum.
        """
        computed = (
            self.technology_relevance
            + self.internship_likelihood
            + self.skills_match
            + self.maturity
            + self.contactability
            + self.geographic
        )
        if computed != self.total_score:
            logger.warning(
                "AI arithmetic was wrong: reported %d, actual sum %d. Using %d.",
                self.total_score, computed, computed,
            )
            object.__setattr__(self, "total_score", computed)
        return self


# --- Turning the result into spreadsheet columns ---------------------------

def to_company_updates(result: Qualification) -> dict[str, Any]:
    """Map a validated result onto Companies-sheet column names."""
    return {
        "What They Do": result.what_they_do,
        "Technology Function": result.technology_function,
        "Internship Evidence": result.internship_evidence,
        "Careers URL": result.careers_url,
        "Relevant Roles": "; ".join(result.relevant_roles) or schema.UNKNOWN,
        "Technology Score": result.technology_relevance,
        "Internship Score": result.internship_likelihood,
        "Skills Match Score": result.skills_match,
        "Maturity Score": result.maturity,
        "Contactability Score": result.contactability,
        "Geographic Score": result.geographic,
        "Total Score": result.total_score,
        "Priority": result.priority,
        "Qualification Reason": result.reason,
        "Research Status": result.classification,
    }


# --- Prompt building -------------------------------------------------------

def load_prompt_template() -> str:
    """Read the qualification prompt from disk."""
    if not PROMPT_PATH.exists():
        raise QualificationError(
            f"Prompt file missing: {PROMPT_PATH}. "
            "It should have been created in Stage 3."
        )
    return PROMPT_PATH.read_text(encoding="utf-8")


def format_student_profile(settings: dict[str, str]) -> str:
    """Describe the student from the Settings sheet, with no embellishment.

    Only fields that are actually filled in are included. FILL_IN
    placeholders are skipped rather than sent to the AI as if real.
    """
    wanted = [
        "Name", "University", "Degree", "Graduation",
        "Target Roles", "Skills", "Internship Type",
        "Preferred Locations", "Availability", "Experience Level",
    ]
    lines = []
    for key in wanted:
        value = str(settings.get(key, "")).strip()
        if value and value != "FILL_IN":
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) or "- (no profile recorded)"


def build_prompt(
    company: dict[str, Any], pack: EvidencePack, settings: dict[str, str]
) -> str:
    """Fill the template with one company's details and its evidence."""
    template = load_prompt_template()
    return template.format(
        student_profile=format_student_profile(settings),
        company_name=company.get("Company Name", schema.UNKNOWN),
        website=company.get("Website") or schema.UNKNOWN,
        country=company.get("Country") or schema.UNKNOWN,
        region=company.get("Region") or schema.UNKNOWN,
        evidence=pack.to_prompt_text(),
    )


# --- Parsing the AI reply --------------------------------------------------

def extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of the model's reply.

    Models sometimes wrap JSON in ```json fences or add a sentence before
    it, despite instructions. Rather than fail, we strip fences and fall
    back to the outermost {...} span.
    """
    text = raw.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise QualificationError(
                f"AI reply contained no valid JSON: {exc}"
            ) from exc

    raise QualificationError(
        f"AI reply contained no JSON object. First 200 chars: {raw[:200]!r}"
    )


def parse_result(raw: str) -> Qualification:
    """Parse and validate one AI reply."""
    data = extract_json(raw)
    try:
        return Qualification(**data)
    except ValidationError as exc:
        # Report the specific field problems, not a wall of pydantic output.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise QualificationError(f"AI output failed validation -- {problems}") from exc


# --- Calling the AI --------------------------------------------------------

def call_ai(config: Config, prompt: str) -> str:
    """Send the prompt to whichever provider is configured.

    The provider-specific code lives in app/ai.py; this wrapper exists so
    qualify_company has a stable default caller.
    """
    caller = get_ai_caller(config)
    try:
        return caller(config, prompt)
    except AIError as exc:
        raise QualificationError(str(exc)) from exc


def qualify_company(
    config: Config,
    company: dict[str, Any],
    pack: EvidencePack,
    settings: dict[str, str],
    ai_caller=call_ai,
    retries: int = 1,
) -> Qualification:
    """Qualify one company, retrying once if the AI returns bad output.

    `ai_caller` is injected so tests and --mock can supply a fake without
    touching the network.
    """
    prompt = build_prompt(company, pack, settings)
    name = company.get("Company Name", "?")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ai_caller(config, prompt)
            return parse_result(raw)
        except AIError as exc:
            # A provider failure (bad key, overload, network) is not a
            # malformed-output problem, so it is not retried here --
            # app.ai already retried the transient ones. Convert it so
            # the caller has one exception type to handle.
            raise QualificationError(str(exc)) from exc
        except QualificationError as exc:
            last_error = exc
            if attempt < retries:
                logger.warning(
                    "%s: attempt %d failed (%s). Retrying.", name, attempt + 1, exc
                )
            else:
                logger.error("%s: giving up after %d attempts.", name, attempt + 1)

    raise QualificationError(f"{name}: {last_error}")

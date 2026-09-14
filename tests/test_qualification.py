"""Stage 3 tests: bad AI output must never reach the workbook.

These are the most important tests in the project so far. The AI is the
component most likely to misbehave, and pydantic is what stops it.

Run with:   python -m unittest discover tests
"""

import json
import unittest

from app import schema
from app.qualification import (
    Qualification,
    QualificationError,
    extract_json,
    format_student_profile,
    parse_result,
    qualify_company,
    to_company_updates,
)
from app.research import EvidencePack, Page


def valid_payload(**overrides):
    """A well-formed AI reply, which individual tests then corrupt."""
    payload = {
        "classification": "QUALIFY",
        "confidence": 0.9,
        "technology_relevance": 22,
        "internship_likelihood": 18,
        "skills_match": 16,
        "maturity": 9,
        "contactability": 8,
        "geographic": 9,
        "total_score": 82,
        "priority": "B",
        "reason": "Careers page lists an internship programme.",
        "evidence": ["VERIFIED: careers page mentions Internship Program"],
        "relevant_roles": ["Data Analyst Intern"],
        "suggested_contact_type": "Internship/Graduate Recruitment",
        "what_they_do": "Telecommunications and mobile money.",
        "technology_function": "Engineering and data teams.",
        "internship_evidence": "Internship Program named on careers page.",
        "careers_url": "https://example.com/careers",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestJsonExtraction(unittest.TestCase):
    def test_plain_json_parses(self):
        self.assertEqual(extract_json('{"a": 1}')["a"], 1)

    def test_markdown_fences_are_stripped(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```')["a"], 1)

    def test_surrounding_prose_is_tolerated(self):
        raw = 'Here is my assessment:\n{"a": 1}\nHope that helps!'
        self.assertEqual(extract_json(raw)["a"], 1)

    def test_no_json_at_all_raises_clearly(self):
        with self.assertRaises(QualificationError):
            extract_json("I am unable to assess this company.")


class TestValidationGate(unittest.TestCase):
    """Every one of these represents an AI failure we must catch."""

    def test_valid_output_is_accepted(self):
        result = parse_result(valid_payload())
        self.assertEqual(result.classification, "QUALIFY")
        self.assertEqual(result.total_score, 82)

    def test_score_above_its_maximum_is_rejected(self):
        # 30 out of a possible 25.
        with self.assertRaises(QualificationError):
            parse_result(valid_payload(technology_relevance=30))

    def test_negative_score_is_rejected(self):
        with self.assertRaises(QualificationError):
            parse_result(valid_payload(maturity=-5))

    def test_invented_classification_is_rejected(self):
        with self.assertRaises(QualificationError):
            parse_result(valid_payload(classification="MAYBE"))

    def test_invented_priority_is_rejected(self):
        with self.assertRaises(QualificationError):
            parse_result(valid_payload(priority="A+"))

    def test_confidence_outside_zero_to_one_is_rejected(self):
        with self.assertRaises(QualificationError):
            parse_result(valid_payload(confidence=1.5))

    def test_missing_required_field_is_rejected(self):
        payload = json.loads(valid_payload())
        del payload["classification"]
        with self.assertRaises(QualificationError):
            parse_result(json.dumps(payload))

    def test_bad_arithmetic_is_corrected_not_trusted(self):
        # Model claims 99 but the parts sum to 82.
        result = parse_result(valid_payload(total_score=99))
        self.assertEqual(result.total_score, 82)

    def test_lowercase_classification_is_normalised(self):
        result = parse_result(valid_payload(classification="qualify"))
        self.assertEqual(result.classification, "QUALIFY")


class TestExcelMapping(unittest.TestCase):
    def test_every_mapped_column_exists_in_the_schema(self):
        updates = to_company_updates(parse_result(valid_payload()))
        for column in updates:
            self.assertIn(column, schema.COMPANY_COLUMNS, f"unknown column {column!r}")

    def test_roles_are_joined_for_a_single_cell(self):
        updates = to_company_updates(parse_result(valid_payload()))
        self.assertEqual(updates["Relevant Roles"], "Data Analyst Intern")

    def test_empty_roles_become_unknown_not_blank(self):
        updates = to_company_updates(parse_result(valid_payload(relevant_roles=[])))
        self.assertEqual(updates["Relevant Roles"], schema.UNKNOWN)


class TestEvidencePack(unittest.TestCase):
    def test_empty_pack_tells_the_ai_there_is_no_evidence(self):
        pack = EvidencePack(company_name="Test Ltd")
        pack.notes.append("domain does not resolve")
        text = pack.to_prompt_text()
        self.assertIn("NO EVIDENCE RETRIEVED", text)
        self.assertIn("domain does not resolve", text)

    def test_pack_with_pages_includes_source_urls(self):
        pack = EvidencePack(company_name="Test Ltd")
        pack.pages.append(Page(url="https://x.com", text="We build software."))
        text = pack.to_prompt_text()
        self.assertIn("https://x.com", text)
        self.assertIn("We build software.", text)


class TestStudentProfile(unittest.TestCase):
    def test_unfilled_placeholders_are_not_sent_to_the_ai(self):
        profile = format_student_profile(
            {"Name": "Monica Wughanga Masae", "LinkedIn": "FILL_IN"}
        )
        self.assertIn("Monica Wughanga Masae", profile)
        self.assertNotIn("FILL_IN", profile)


class TestRetryBehaviour(unittest.TestCase):
    def test_a_transient_bad_reply_is_retried_then_succeeds(self):
        replies = ["not json at all", valid_payload()]

        def flaky_caller(config, prompt):
            return replies.pop(0)

        result = qualify_company(
            config=None,
            company={"Company Name": "Test Ltd"},
            pack=EvidencePack(company_name="Test Ltd"),
            settings={"Name": "Monica Wughanga Masae"},
            ai_caller=flaky_caller,
        )
        self.assertEqual(result.classification, "QUALIFY")

    def test_persistent_failure_raises_rather_than_returning_junk(self):
        def broken_caller(config, prompt):
            return "still not json"

        with self.assertRaises(QualificationError):
            qualify_company(
                config=None,
                company={"Company Name": "Test Ltd"},
                pack=EvidencePack(company_name="Test Ltd"),
                settings={},
                ai_caller=broken_caller,
            )


if __name__ == "__main__":
    unittest.main()


class TestPriorityDerivation(unittest.TestCase):
    """Priority is arithmetic, so Python owns it -- not the AI."""

    def test_bands_match_the_specification(self):
        from app.qualification import priority_for_score

        for score, expected in [
            (100, "A"), (90, "A"), (89, "B"), (75, "B"),
            (74, "C"), (60, "C"), (59, "D"), (40, "D"), (39, "REJECT"), (0, "REJECT"),
        ]:
            self.assertEqual(priority_for_score(score), expected, f"score {score}")

    def test_ai_priority_is_overridden_by_the_real_band(self):
        # AI claims "A" but the scores total 82, which is a B.
        result = parse_result(valid_payload(priority="A"))
        self.assertEqual(result.priority, "B")

    def test_needs_review_never_gets_labelled_reject(self):
        """A company we could not research is not a company we turned down."""
        result = parse_result(
            valid_payload(
                classification="NEEDS_REVIEW",
                priority="REJECT",
                technology_relevance=10, internship_likelihood=5,
                skills_match=8, maturity=4, contactability=3, geographic=5,
                total_score=35,
            )
        )
        self.assertEqual(result.priority, "NEEDS_REVIEW")

    def test_reject_classification_always_gets_reject_priority(self):
        result = parse_result(valid_payload(classification="REJECT", priority="A"))
        self.assertEqual(result.priority, "REJECT")

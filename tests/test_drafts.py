"""Stage 6 tests: drafts are honest, and nothing is ever auto-approved.

Run with:   python -m unittest discover tests
"""

import json
import unittest

from app import schema
from app.campaigns import AI_ML, DATA, GENERAL, SOFTWARE, choose_campaign
from app.email_drafts import (
    Draft,
    DraftError,
    build_signature,
    check_quality,
    existing_outreach_keys,
    generate_draft,
    to_outreach_row,
)

GOOD_BODY = (
    "Dear Hiring Team,\n\n"
    "I am Monica Wughanga Masae, a BSc Data Science student at the Open "
    "University of Kenya, expecting to graduate in 2027. I am writing to ask "
    "about an internship or industrial attachment.\n\n"
    "I understand you work on telecommunications and mobile money platforms, "
    "and your careers page lists an internship programme. That is the kind of "
    "data and software work I would like to learn in.\n\n"
    "Through my coursework and personal projects I have worked with Python, "
    "SQL, R and Excel, and I have built small data analysis and visualisation "
    "projects. I would be glad to support reporting or data preparation work "
    "while I learn from your team.\n\n"
    "Would you consider an intern or attachment placement? If this is not "
    "your area, I would be grateful if you could point me to the right "
    "person.\n\n"
    "Thank you for your time."
)


def payload(**overrides):
    data = {
        "subject": "Internship enquiry - BSc Data Science student",
        "body": GOOD_BODY,
        "grounded_claims": ["telecommunications: from What They Do"],
        "confidence": 0.8,
    }
    data.update(overrides)
    return json.dumps(data)


class TestQualityChecks(unittest.TestCase):
    """These catch what the prompt asks for but cannot guarantee."""

    def test_a_good_draft_passes(self):
        self.assertEqual(check_quality("Internship enquiry", GOOD_BODY), [])

    def test_flattery_is_caught(self):
        problems = check_quality(
            "Hello", GOOD_BODY + "\nI have always admired your innovative company."
        )
        self.assertTrue(any("flattery" in p for p in problems), problems)

    def test_overclaiming_experience_is_caught(self):
        problems = check_quality(
            "Hello",
            GOOD_BODY + "\nAs an experienced data scientist with years of experience.",
        )
        self.assertTrue(any("overstates" in p for p in problems), problems)

    def test_marketing_words_are_caught(self):
        for phrase in ["I am passionate about", "cutting-edge", "world-class"]:
            problems = check_quality("Hi", GOOD_BODY + f"\n{phrase} solutions.")
            self.assertTrue(problems, f"{phrase!r} was not caught")

    def test_exclamation_marks_are_caught(self):
        problems = check_quality("Hi", GOOD_BODY + "\nThank you!")
        self.assertTrue(any("exclamation" in p for p in problems), problems)

    def test_an_invented_person_name_in_the_greeting_is_caught(self):
        body = GOOD_BODY.replace("Dear Hiring Team,", "Dear Mr James Mwangi,")
        problems = check_quality("Hi", body)
        self.assertTrue(any("named person" in p for p in problems), problems)

    def test_team_greetings_are_accepted(self):
        for greeting in ["Dear Hiring Team,", "Dear Safaricom Team,", "Dear HR,"]:
            body = GOOD_BODY.replace("Dear Hiring Team,", greeting)
            problems = [p for p in check_quality("Hi", body) if "named person" in p]
            self.assertEqual(problems, [], f"{greeting!r} was wrongly flagged")

    def test_invented_year_of_study_is_caught(self):
        """Settings records a graduation year, never a current year."""
        problems = check_quality(
            "Hi", GOOD_BODY + "\nI am a second-year student at the university."
        )
        self.assertTrue(any("invents a detail" in p for p in problems), problems)

    def test_graduation_year_wording_is_not_flagged(self):
        body = GOOD_BODY + "\nI expect to graduate in 2027."
        problems = [p for p in check_quality("Hi", body) if "invents a detail" in p]
        self.assertEqual(problems, [])

    def test_an_overlong_draft_is_caught(self):
        problems = check_quality("Hi", "word " * 300)
        self.assertTrue(any("too long" in p for p in problems), problems)


class TestApprovalGate(unittest.TestCase):
    """The single most important rule: nothing is ever auto-approved."""

    def _row(self, problems=None):
        draft = Draft(
            subject="Internship enquiry", body=GOOD_BODY, confidence=0.9
        )
        return to_outreach_row(
            "O001",
            {"Company ID": "C001", "Company Name": "Test Ltd"},
            {"Contact ID": "P001", "Contact Name": schema.UNKNOWN},
            draft, DATA, problems or [], "2026-01-01",
        )

    def test_every_draft_starts_pending(self):
        self.assertEqual(self._row()["Approval Status"], schema.APPROVAL_PENDING)

    def test_email_status_is_drafted_not_sent(self):
        self.assertEqual(self._row()["Email Status"], schema.EMAIL_DRAFTED)

    def test_no_send_date_is_set(self):
        row = self._row()
        self.assertNotIn("Date Sent", row)

    def test_reply_status_starts_at_no_reply(self):
        self.assertEqual(self._row()["Reply Status"], schema.REPLY_NONE)

    def test_quality_problems_are_recorded_in_notes(self):
        row = self._row(problems=["flattery: 'i admire'"])
        self.assertIn("REVIEW CAREFULLY", row["Notes"])
        # A flagged draft is still PENDING -- flagging must not auto-reject.
        self.assertEqual(row["Approval Status"], schema.APPROVAL_PENDING)

    def test_all_columns_exist_in_the_schema(self):
        for column in self._row():
            self.assertIn(column, schema.OUTREACH_COLUMNS, f"unknown column {column!r}")


class TestSignOff(unittest.TestCase):
    """The signature supplies the name, so the body must not repeat it."""

    def test_a_trailing_name_is_removed(self):
        from app.email_drafts import strip_trailing_name

        body = "Thank you for your time.\nSincerely,\nMonica Wughanga Masae"
        self.assertEqual(
            strip_trailing_name(body, "Monica Wughanga Masae"),
            "Thank you for your time.\nSincerely,",
        )

    def test_a_body_without_a_name_is_untouched(self):
        from app.email_drafts import strip_trailing_name

        body = "Thank you for your time."
        self.assertEqual(strip_trailing_name(body, "Monica Wughanga Masae"), body)


class TestSignature(unittest.TestCase):
    def test_unfilled_fields_are_omitted_not_invented(self):
        signature = build_signature(
            {"Name": "Monica Wughanga Masae", "Email": "m@example.com",
             "Portfolio": "FILL_IN", "GitHub": ""}
        )
        self.assertIn("m@example.com", signature)
        self.assertNotIn("FILL_IN", signature)
        self.assertNotIn("Portfolio", signature)

    def test_real_fields_appear(self):
        signature = build_signature(
            {"Name": "Monica Wughanga Masae", "Degree": "BSc Data Science",
             "University": "Open University of Kenya", "Graduation": "2027"}
        )
        self.assertIn("BSc Data Science", signature)
        self.assertIn("2027", signature)


class TestCampaignChoice(unittest.TestCase):
    def test_a_data_company_gets_the_data_campaign(self):
        self.assertEqual(
            choose_campaign({"What They Do": "We provide business intelligence and analytics"}),
            DATA,
        )

    def test_a_software_company_gets_the_software_campaign(self):
        self.assertEqual(
            choose_campaign({"What They Do": "We build software and cloud platforms"}),
            SOFTWARE,
        )

    def test_an_ai_company_gets_the_ai_campaign(self):
        self.assertEqual(
            choose_campaign({"Technology Function": "machine learning research"}), AI_ML
        )

    def test_no_evidence_falls_back_to_general(self):
        self.assertEqual(choose_campaign({"What They Do": schema.UNKNOWN}), GENERAL)

    def test_student_roles_do_not_drive_the_campaign(self):
        """Relevant Roles describes the student, not the company."""
        company = {
            "What They Do": "We operate a chain of retail pharmacies",
            "Relevant Roles": "Data Science Intern; Machine Learning Intern",
        }
        self.assertNotEqual(choose_campaign(company), AI_ML)


class TestDuplicatePrevention(unittest.TestCase):
    def test_same_contact_same_campaign_is_a_duplicate(self):
        existing = [{"Company ID": "C001", "Contact ID": "P001", "Campaign": DATA}]
        self.assertIn(("C001", "P001", DATA), existing_outreach_keys(existing))

    def test_a_different_campaign_is_not_a_duplicate(self):
        existing = [{"Company ID": "C001", "Contact ID": "P001", "Campaign": DATA}]
        self.assertNotIn(("C001", "P001", SOFTWARE), existing_outreach_keys(existing))


class TestGeneration(unittest.TestCase):
    def test_a_flawed_draft_is_returned_flagged_not_discarded(self):
        """A human reviewer needs to see it; silently dropping it is worse."""
        bad = payload(body=GOOD_BODY + "\nI have always admired your innovative company.")

        def always_bad(config, prompt):
            return bad

        draft, campaign, problems = generate_draft(
            None,
            {"Company Name": "Test Ltd", "What They Do": "software"},
            {"Name": "Monica Wughanga Masae"},
            always_bad,
        )
        self.assertTrue(problems)
        self.assertIn("admired", draft.body)

    def test_a_bad_first_draft_is_retried(self):
        replies = [
            payload(body=GOOD_BODY + "\nI am passionate about your work."),
            payload(),
        ]

        def flaky(config, prompt):
            return replies.pop(0)

        draft, campaign, problems = generate_draft(
            None, {"Company Name": "Test Ltd", "What They Do": "software"},
            {"Name": "Monica Wughanga Masae"}, flaky,
        )
        self.assertEqual(problems, [])

    def test_persistent_failure_raises(self):
        def broken(config, prompt):
            return "not json"

        with self.assertRaises(DraftError):
            generate_draft(
                None, {"Company Name": "Test Ltd"}, {}, broken
            )


if __name__ == "__main__":
    unittest.main()


class TestAlreadyContactedCompanies(unittest.TestCase):
    """A company you have already written to must not be drafted again.

    The campaign-level check was not enough. Applying to a company
    yourself and logging it with `log-application` creates a row with the
    campaign "Applied outside the system", so a later draft run saw a
    different campaign and wrote a second email. That happened to Rudder
    Research, which had already received an application.
    """

    def test_a_sent_company_is_blocked(self):
        from app.email_drafts import already_contacted_companies

        outreach = [{
            "Outreach ID": "O029", "Company ID": "C043",
            "Email Status": schema.EMAIL_SENT, "Date Sent": "2026-09-14",
            "Reply Status": schema.REPLY_NONE,
        }]
        contacted = already_contacted_companies(outreach)
        self.assertIn("C043", contacted)
        self.assertIn("already contacted", contacted["C043"])

    def test_a_manual_application_blocks_a_later_draft(self):
        """The exact bug: a different campaign label must not slip past."""
        from app.email_drafts import already_contacted_companies

        outreach = [{
            "Outreach ID": "O029", "Company ID": "C043",
            "Campaign": "Applied outside the system",
            "Email Status": schema.EMAIL_SENT, "Date Sent": "2026-09-14",
            "Reply Status": schema.REPLY_NONE,
        }]
        self.assertIn("C043", already_contacted_companies(outreach))

    def test_a_company_with_a_reply_is_blocked(self):
        from app.email_drafts import already_contacted_companies

        outreach = [{
            "Outreach ID": "O003", "Company ID": "C006",
            "Email Status": schema.EMAIL_SENT, "Reply Status": "POSITIVE",
        }]
        self.assertIn("replied", already_contacted_companies(outreach)["C006"])

    def test_an_existing_draft_is_flagged(self):
        from app.email_drafts import already_contacted_companies

        outreach = [{
            "Outreach ID": "O005", "Company ID": "C010",
            "Email Status": schema.EMAIL_DRAFTED, "Reply Status": schema.REPLY_NONE,
        }]
        self.assertIn("draft already exists", already_contacted_companies(outreach)["C010"])

    def test_an_untouched_company_is_not_blocked(self):
        from app.email_drafts import already_contacted_companies

        self.assertEqual(already_contacted_companies([]), {})

    def test_rows_without_a_company_id_are_ignored(self):
        from app.email_drafts import already_contacted_companies

        outreach = [{"Outreach ID": "O099", "Company ID": "",
                     "Email Status": schema.EMAIL_SENT}]
        self.assertEqual(already_contacted_companies(outreach), {})

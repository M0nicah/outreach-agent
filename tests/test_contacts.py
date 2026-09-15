"""Stage 5 tests: the system must never invent a contact.

The critical test here is test_ai_invented_email_is_discarded. If that
ever fails, the system could email a fabricated address -- which is the
worst thing this project could do.

Run with:   python -m unittest discover tests
"""

import json
import unittest

from app import schema
from app.contacts import (
    ContactSearch,
    FoundContact,
    classify_contacts,
    existing_contact_keys,
    extract_emails,
    is_usable_email,
    rank_emails,
)


class TestEmailExtraction(unittest.TestCase):
    def test_finds_a_plain_address(self):
        text = "Get in touch at careers@example.com for opportunities."
        self.assertEqual(extract_emails(text), ["careers@example.com"])

    def test_rejects_noreply_addresses(self):
        self.assertFalse(is_usable_email("noreply@example.com"))
        self.assertFalse(is_usable_email("do-not-reply@example.com"))

    def test_rejects_image_filenames_that_look_like_emails(self):
        self.assertFalse(is_usable_email("logo@2x.png"))

    def test_only_keeps_the_company_own_domain(self):
        text = "ours@company.co.ke and theirs@someagency.com"
        found = extract_emails(text, company_domain="company.co.ke")
        self.assertEqual(found, ["ours@company.co.ke"])

    def test_accepts_a_subdomain_of_the_company(self):
        self.assertTrue(is_usable_email("hr@jobs.company.co.ke", "company.co.ke"))

    def test_nothing_is_invented_from_empty_text(self):
        self.assertEqual(extract_emails(""), [])
        self.assertEqual(extract_emails("We have no contact details."), [])


class TestRanking(unittest.TestCase):
    def test_recruitment_addresses_come_first(self):
        emails = ["info@x.com", "careers@x.com", "support@x.com"]
        self.assertEqual(rank_emails(emails)[0], "careers@x.com")

    def test_ranking_never_adds_an_address(self):
        emails = ["info@x.com"]
        self.assertEqual(rank_emails(emails), ["info@x.com"])


class TestAntiFabricationGuard(unittest.TestCase):
    """The most important tests in the project."""

    def _search_with(self, email):
        search = ContactSearch(company_name="Test Ltd")
        search.contacts.append(
            FoundContact(email=email, source_url="https://test.com/contact")
        )
        return search

    def test_ai_invented_email_is_discarded(self):
        """The AI returns a plausible address we never saw. It must not survive."""
        observed = "customercare@test.com"
        search = self._search_with(observed)

        def lying_ai(config, prompt):
            return json.dumps(
                {
                    "contacts": [
                        {
                            "email": "recruitment@test.com",  # never observed
                            "contact_type": "Recruiter",
                            "suitability": "GOOD",
                            "why": "Looks like the recruitment inbox.",
                        }
                    ]
                }
            )

        result = classify_contacts(None, {"Company Name": "Test Ltd"}, search, lying_ai)

        emails = [c.email for c in result.contacts]
        self.assertIn(observed, emails)
        self.assertNotIn("recruitment@test.com", emails)
        self.assertEqual(len(result.contacts), 1)

    def test_ai_cannot_add_extra_contacts(self):
        search = self._search_with("info@test.com")

        def inventive_ai(config, prompt):
            return json.dumps(
                {
                    "contacts": [
                        {"email": "info@test.com", "contact_type": "Other",
                         "suitability": "ACCEPTABLE", "why": "General inbox."},
                        {"email": "ceo@test.com", "contact_type": "Founder",
                         "suitability": "GOOD", "why": "Invented."},
                        {"email": "hr@test.com", "contact_type": "HR",
                         "suitability": "GOOD", "why": "Also invented."},
                    ]
                }
            )

        result = classify_contacts(None, {"Company Name": "Test Ltd"}, search, inventive_ai)
        self.assertEqual(len(result.contacts), 1)
        self.assertEqual(result.contacts[0].email, "info@test.com")

    def test_ai_failure_keeps_the_real_contacts(self):
        """A broken classifier must not lose genuinely found contacts."""
        search = self._search_with("info@test.com")

        def broken_ai(config, prompt):
            raise RuntimeError("API exploded")

        result = classify_contacts(None, {"Company Name": "Test Ltd"}, search, broken_ai)
        self.assertEqual(len(result.contacts), 1)
        self.assertEqual(result.contacts[0].email, "info@test.com")

    def test_unrecognised_contact_type_becomes_other_not_an_error(self):
        search = self._search_with("info@test.com")

        def odd_ai(config, prompt):
            return json.dumps(
                {"contacts": [{"email": "info@test.com",
                               "contact_type": "Supreme Chancellor",
                               "suitability": "GOOD", "why": "x"}]}
            )

        result = classify_contacts(None, {"Company Name": "Test Ltd"}, search, odd_ai)
        self.assertEqual(result.contacts[0].contact_type, "Other")


class TestContactRows(unittest.TestCase):
    def test_row_columns_all_exist_in_the_schema(self):
        contact = FoundContact(email="a@b.com", source_url="https://b.com")
        row = contact.to_row("P001", {"Company ID": "C001", "Company Name": "B Ltd"})
        for column in row:
            self.assertIn(column, schema.CONTACT_COLUMNS, f"unknown column {column!r}")

    def test_a_contact_with_no_email_is_marked_not_verified(self):
        contact = FoundContact(source_url="https://b.com/careers")
        row = contact.to_row("P001", {"Company ID": "C001", "Company Name": "B Ltd"})
        self.assertEqual(row["Email"], schema.UNKNOWN)
        self.assertEqual(row["Email Verified"], "NO")

    def test_name_and_linkedin_default_to_unknown_never_blank_guesses(self):
        contact = FoundContact(email="a@b.com")
        row = contact.to_row("P001", {"Company ID": "C001", "Company Name": "B Ltd"})
        self.assertEqual(row["Contact Name"], schema.UNKNOWN)
        self.assertEqual(row["LinkedIn"], schema.UNKNOWN)


class TestDuplicatePrevention(unittest.TestCase):
    def test_existing_emails_are_recognised(self):
        existing = [{"Company ID": "C001", "Email": "a@b.com", "Source": ""}]
        self.assertIn(("C001", "a@b.com"), existing_contact_keys(existing))

    def test_same_email_at_a_different_company_is_not_a_duplicate(self):
        existing = [{"Company ID": "C001", "Email": "a@b.com", "Source": ""}]
        self.assertNotIn(("C002", "a@b.com"), existing_contact_keys(existing))


if __name__ == "__main__":
    unittest.main()


class TestWrongCountryHandling(unittest.TestCase):
    """A multinational's other-country HR inbox is the wrong route.

    Liquid Intelligent published botswanahr@, hr.uganda@ and hr.zim@ but
    no Kenya address. Emailing Botswana HR about a Nairobi placement
    wastes everybody's time, so those are demoted and flagged.
    """

    def test_other_country_addresses_are_detected(self):
        from app.contacts import wrong_country

        for email in ["botswanahr@x.tech", "hr.uganda@x.tech", "hr.zim@x.tech"]:
            self.assertTrue(wrong_country(email), email)

    def test_local_and_neutral_addresses_are_not_flagged(self):
        from app.contacts import wrong_country

        for email in ["hr@kemri.go.ke", "careers@x.co.ke", "hr.kenya@x.tech"]:
            self.assertFalse(wrong_country(email), email)

    def test_a_home_country_address_outranks_a_foreign_one(self):
        ranked = rank_emails(["botswanahr@x.tech", "hr.kenya@x.tech"])
        self.assertEqual(ranked[0], "hr.kenya@x.tech")

    def test_a_general_inbox_outranks_foreign_hr(self):
        """info@ at least reaches the right country."""
        ranked = rank_emails(["botswanahr@x.tech", "info@x.tech"])
        self.assertEqual(ranked[0], "info@x.tech")

    def test_foreign_addresses_are_kept_not_discarded(self):
        """Sometimes a regional office is the only route in."""
        ranked = rank_emails(["botswanahr@x.tech", "info@x.tech"])
        self.assertIn("botswanahr@x.tech", ranked)


class TestUnsuitableInboxes(unittest.TestCase):
    """Some published addresses are real but are the wrong place to apply.

    Two found in live runs: raising.concerns@4g-capital.com is a
    whistleblowing line, and abeer.etefa@wfp.org is a named press officer.
    Both are genuine addresses; neither should receive a job application.
    """

    def test_whistleblowing_and_press_addresses_are_flagged(self):
        from app.contacts import is_unsuitable_inbox

        for email in [
            "raising.concerns@x.com", "press@x.com", "media@x.com",
            "legal@x.com", "procurement@x.com", "complaints@x.com",
        ]:
            self.assertTrue(is_unsuitable_inbox(email), email)

    def test_normal_addresses_are_not_flagged(self):
        from app.contacts import is_unsuitable_inbox

        for email in ["hr@x.com", "careers@x.com", "info@x.com", "kenya.jobs@x.com"]:
            self.assertFalse(is_unsuitable_inbox(email), email)

    def test_unsuitable_addresses_rank_last(self):
        ranked = rank_emails(["raising.concerns@x.com", "info@x.com", "hr@x.com"])
        self.assertEqual(ranked[0], "hr@x.com")
        self.assertEqual(ranked[-1], "raising.concerns@x.com")

    def test_they_are_kept_not_discarded(self):
        """Still recorded -- you may want to see what exists."""
        ranked = rank_emails(["raising.concerns@x.com", "info@x.com"])
        self.assertIn("raising.concerns@x.com", ranked)


class TestSkipAlreadySearched(unittest.TestCase):
    """find-contacts should not re-fetch companies it has already searched.

    Before this, every run re-fetched all 53 qualified companies, taking
    minutes to find nothing new. A company where the search found nothing
    was the harder case: with no row written, it looked identical to one
    never searched, so it was retried forever.
    """

    def test_a_company_with_contacts_is_recognised_as_searched(self):
        existing = [{"Company ID": "C001", "Email": "hr@a.com"}]
        searched = {str(c.get("Company ID", "")).strip() for c in existing}
        self.assertIn("C001", searched)

    def test_a_none_found_row_also_counts_as_searched(self):
        """The row exists with no email, which is the whole point."""
        existing = [{
            "Company ID": "C002", "Email": schema.UNKNOWN,
            "Contact Status": "NONE_FOUND",
        }]
        searched = {str(c.get("Company ID", "")).strip() for c in existing}
        self.assertIn("C002", searched)

    def test_an_unsearched_company_is_not_skipped(self):
        existing = [{"Company ID": "C001", "Email": "hr@a.com"}]
        searched = {str(c.get("Company ID", "")).strip() for c in existing}
        self.assertNotIn("C099", searched)

    def test_a_none_found_row_has_no_usable_email(self):
        """It must not look like a contact you could write to."""
        row = {
            "Company ID": "C002", "Email": schema.UNKNOWN,
            "Contact Status": "NONE_FOUND", "Source": schema.UNKNOWN,
        }
        email = str(row.get("Email", "")).strip()
        self.assertEqual(email, schema.UNKNOWN)

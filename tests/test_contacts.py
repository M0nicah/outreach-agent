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

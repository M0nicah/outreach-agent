"""Stage 8 tests: the sending safety guards.

No test here touches the network. They verify the guards that stand
between an approved draft and a stranger's inbox.

Run with:   python -m unittest discover tests
"""

import unittest

from app import schema
from app.gmail import (
    GmailError,
    build_message,
    failed_updates,
    is_valid_email,
    remaining_today,
    sent_today,
    sent_updates,
)


class FakeConfig:
    daily_send_limit = 10


class TestAddressValidation(unittest.TestCase):
    def test_real_addresses_pass(self):
        for address in ["hr@company.co.ke", "a.b+c@sub.domain.com"]:
            self.assertTrue(is_valid_email(address), address)

    def test_unknown_is_never_a_valid_recipient(self):
        self.assertFalse(is_valid_email(schema.UNKNOWN))
        self.assertFalse(is_valid_email(""))
        self.assertFalse(is_valid_email(None))

    def test_malformed_addresses_are_rejected(self):
        for address in ["not an email", "@company.com", "hr@", "hr@com"]:
            self.assertFalse(is_valid_email(address), address)


class TestMessageBuilding(unittest.TestCase):
    def test_a_valid_message_is_built(self):
        payload = build_message("hr@test.com", "Subject", "Body text")
        self.assertIn("raw", payload)

    def test_an_invalid_recipient_is_refused(self):
        with self.assertRaises(GmailError):
            build_message(schema.UNKNOWN, "Subject", "Body")

    def test_an_empty_subject_is_refused(self):
        with self.assertRaises(GmailError):
            build_message("hr@test.com", "   ", "Body")

    def test_an_empty_body_is_refused(self):
        with self.assertRaises(GmailError):
            build_message("hr@test.com", "Subject", "")

    def test_the_recipient_and_subject_survive_encoding(self):
        import base64

        payload = build_message("hr@test.com", "Internship enquiry", "Hello")
        decoded = base64.urlsafe_b64decode(payload["raw"]).decode()
        self.assertIn("hr@test.com", decoded)
        self.assertIn("Internship enquiry", decoded)


class TestDailyLimit(unittest.TestCase):
    """The limit is counted from the workbook, so it survives restarts."""

    def rows(self, *dates):
        return [
            {"Email Status": schema.EMAIL_SENT, "Date Sent": d} for d in dates
        ]

    def test_counts_only_today(self):
        rows = self.rows("2026-09-14", "2026-09-14", "2026-09-13")
        self.assertEqual(sent_today(rows, today="2026-09-14"), 2)

    def test_unsent_rows_do_not_count(self):
        rows = [{"Email Status": schema.EMAIL_DRAFTED, "Date Sent": "2026-09-14"}]
        self.assertEqual(sent_today(rows, today="2026-09-14"), 0)

    def test_failed_rows_do_not_count_against_the_limit(self):
        rows = [{"Email Status": schema.EMAIL_FAILED, "Date Sent": "2026-09-14"}]
        self.assertEqual(sent_today(rows, today="2026-09-14"), 0)

    def test_remaining_never_goes_negative(self):
        rows = self.rows(*["2026-09-14"] * 25)
        import datetime

        today = datetime.date.today().isoformat()
        rows = [{"Email Status": schema.EMAIL_SENT, "Date Sent": today}] * 25
        self.assertEqual(remaining_today(FakeConfig(), rows), 0)


class TestStatusUpdates(unittest.TestCase):
    def test_a_successful_send_is_recorded(self):
        updates = sent_updates("msg123", "2026-09-14")
        self.assertEqual(updates["Email Status"], schema.EMAIL_SENT)
        self.assertEqual(updates["Date Sent"], "2026-09-14")
        self.assertIn("msg123", updates["Notes"])

    def test_a_failure_is_recorded_not_swallowed(self):
        updates = failed_updates("Gmail rejected the recipient")
        self.assertEqual(updates["Email Status"], schema.EMAIL_FAILED)
        self.assertIn("rejected", updates["Notes"])

    def test_a_failure_does_not_set_a_sent_date(self):
        self.assertNotIn("Date Sent", failed_updates("some error"))

    def test_update_columns_exist_in_the_schema(self):
        for updates in [sent_updates("id", "2026-09-14"), failed_updates("x")]:
            for column in updates:
                self.assertIn(column, schema.OUTREACH_COLUMNS, column)


class TestApprovalGateIsReused(unittest.TestCase):
    """Sending must not reimplement the approval decision."""

    def test_gmail_module_does_not_define_its_own_gate(self):
        import app.gmail as gmail_module

        source = open(gmail_module.__file__).read()
        # The sender calls approval.is_sendable; it must not contain its
        # own copy of the rule.
        self.assertNotIn("def is_sendable", source)


if __name__ == "__main__":
    unittest.main()

"""Stage 7 tests: the human approval gate.

These are the tests that make Stage 8 safe to build. The rule under test:

    If Approval Status != APPROVED, the email must NOT be sent,
    even if Email Status says DRAFTED.

Run with:   python -m unittest discover tests
"""

import unittest

from app import schema
from app.approval import (
    ApprovalError,
    apply_decision,
    is_sendable,
    normalise,
    summarise,
)


def row(**overrides):
    """A complete, approved, sendable row that tests then break."""
    base = {
        "Outreach ID": "O001",
        "Company Name": "Test Ltd",
        "Subject": "Internship enquiry",
        "Email Body": "Dear Hiring Team,\n\nI am a student...",
        "Approval Status": schema.APPROVAL_APPROVED,
        "Email Status": schema.EMAIL_APPROVED,
        "Reply Status": schema.REPLY_NONE,
    }
    base.update(overrides)
    return base


class TestTheGate(unittest.TestCase):
    """Only APPROVED may send. Everything else is refused."""

    def test_approved_is_sendable(self):
        allowed, reason = is_sendable(row())
        self.assertTrue(allowed, reason)

    def test_pending_is_refused(self):
        allowed, reason = is_sendable(row(**{"Approval Status": schema.APPROVAL_PENDING}))
        self.assertFalse(allowed)
        self.assertIn("PENDING", reason)

    def test_rejected_is_refused(self):
        allowed, _ = is_sendable(row(**{"Approval Status": schema.APPROVAL_REJECTED}))
        self.assertFalse(allowed)

    def test_edited_is_refused(self):
        """EDITED means you changed it, not that you approved it."""
        allowed, reason = is_sendable(row(**{"Approval Status": schema.APPROVAL_EDITED}))
        self.assertFalse(allowed)
        self.assertIn("did not approve", reason)

    def test_blank_is_refused(self):
        allowed, reason = is_sendable(row(**{"Approval Status": ""}))
        self.assertFalse(allowed)
        self.assertIn("blank", reason)

    def test_drafted_email_status_does_not_override_the_gate(self):
        """The specification calls this out explicitly."""
        allowed, _ = is_sendable(
            row(**{
                "Approval Status": schema.APPROVAL_PENDING,
                "Email Status": schema.EMAIL_DRAFTED,
            })
        )
        self.assertFalse(allowed)

    def test_a_typo_is_refused_not_guessed(self):
        """'APPROVE' is not 'APPROVED'. We stop rather than interpret."""
        for typo in ["APPROVE", "APROVED", "YES", "OK", "APPROVED!"]:
            allowed, reason = is_sendable(row(**{"Approval Status": typo}))
            self.assertFalse(allowed, f"{typo!r} was wrongly accepted")
            self.assertIn("not a valid value", reason)

    def test_case_and_spacing_are_tolerated(self):
        """A human typing in Excel should not be punished for lowercase."""
        for spelling in ["approved", " Approved ", "APPROVED"]:
            allowed, reason = is_sendable(row(**{"Approval Status": spelling}))
            self.assertTrue(allowed, f"{spelling!r} was wrongly refused: {reason}")


class TestDuplicateAndReplyProtection(unittest.TestCase):
    def test_an_already_sent_email_is_not_sent_again(self):
        allowed, reason = is_sendable(row(**{"Email Status": schema.EMAIL_SENT}))
        self.assertFalse(allowed)
        self.assertIn("duplicate", reason)

    def test_a_row_with_a_reply_is_not_sent_again(self):
        allowed, reason = is_sendable(row(**{"Reply Status": "POSITIVE"}))
        self.assertFalse(allowed)
        self.assertIn("reply", reason)

    def test_no_reply_does_not_block(self):
        allowed, _ = is_sendable(row(**{"Reply Status": schema.REPLY_NONE}))
        self.assertTrue(allowed)


class TestContentSafety(unittest.TestCase):
    """An approved row with no content must not send a blank email."""

    def test_empty_subject_is_refused(self):
        allowed, reason = is_sendable(row(Subject=""))
        self.assertFalse(allowed)
        self.assertIn("Subject", reason)

    def test_empty_body_is_refused(self):
        allowed, reason = is_sendable(row(**{"Email Body": "   "}))
        self.assertFalse(allowed)
        self.assertIn("Body", reason)


class TestDecisions(unittest.TestCase):
    def test_approving_sets_the_date_and_email_status(self):
        updates = apply_decision(row(), schema.APPROVAL_APPROVED)
        self.assertEqual(updates["Approval Status"], schema.APPROVAL_APPROVED)
        self.assertEqual(updates["Email Status"], schema.EMAIL_APPROVED)
        self.assertTrue(updates["Date Approved"])

    def test_rejecting_does_not_set_an_approval_date(self):
        updates = apply_decision(row(), schema.APPROVAL_REJECTED)
        self.assertNotIn("Date Approved", updates)

    def test_editing_keeps_it_out_of_the_sendable_set(self):
        updates = apply_decision(row(), schema.APPROVAL_EDITED, edited_body="New text")
        self.assertEqual(updates["Email Body"], "New text")
        # Apply the updates and confirm it still cannot be sent.
        edited = {**row(), **updates}
        allowed, _ = is_sendable(edited)
        self.assertFalse(allowed)

    def test_an_invalid_decision_raises(self):
        with self.assertRaises(ApprovalError):
            apply_decision(row(), "MAYBE")

    def test_decision_columns_exist_in_the_schema(self):
        for decision in schema.APPROVAL_STATUSES:
            for column in apply_decision(row(), decision):
                self.assertIn(column, schema.OUTREACH_COLUMNS, column)


class TestSummary(unittest.TestCase):
    def test_counts_and_buckets_are_correct(self):
        rows = [
            row(**{"Outreach ID": "O001", "Approval Status": schema.APPROVAL_APPROVED}),
            row(**{"Outreach ID": "O002", "Approval Status": schema.APPROVAL_PENDING}),
            row(**{"Outreach ID": "O003", "Approval Status": schema.APPROVAL_REJECTED}),
            row(**{"Outreach ID": "O004", "Approval Status": "APPROVE"}),
        ]
        result = summarise(rows)

        self.assertEqual(len(result["sendable"]), 1)
        self.assertEqual(len(result["invalid"]), 1)
        self.assertEqual(len(result["blocked"]), 2)
        self.assertEqual(result["counts"][schema.APPROVAL_PENDING], 1)

    def test_an_empty_sheet_summarises_cleanly(self):
        result = summarise([])
        self.assertEqual(result["sendable"], [])
        self.assertEqual(result["counts"], {})


class TestNormalise(unittest.TestCase):
    def test_handles_none_and_whitespace(self):
        self.assertEqual(normalise(None), "")
        self.assertEqual(normalise("  approved  "), "APPROVED")


if __name__ == "__main__":
    unittest.main()

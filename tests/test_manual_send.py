"""Tests for manual sending support.

Manual sending must obey the SAME approval gate as automated sending.
The risk being tested here is that the "easier" path quietly becomes a
way to bypass approval.

Run with:   python -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

from app import schema
from app.manual_send import (
    SendRoute,
    build_export,
    mark_sent_updates,
    resolve_route,
    write_export_file,
)


def outreach_row(**overrides):
    base = {
        "Outreach ID": "O001",
        "Company Name": "Test Ltd",
        "Contact ID": "P001",
        "Subject": "Internship enquiry",
        "Email Body": "Dear Hiring Team,\n\nI am a student...",
        "Approval Status": schema.APPROVAL_APPROVED,
        "Email Status": schema.EMAIL_APPROVED,
        "Reply Status": schema.REPLY_NONE,
        "_row": 2,
    }
    base.update(overrides)
    return base


class TestRouteResolution(unittest.TestCase):
    def test_a_real_email_is_an_email_route(self):
        contacts = {"P001": {"Contact ID": "P001", "Email": "hr@test.com"}}
        route, target = resolve_route(outreach_row(), contacts)
        self.assertEqual(route, SendRoute.EMAIL)
        self.assertEqual(target, "hr@test.com")

    def test_unknown_email_falls_back_to_the_portal(self):
        contacts = {
            "P001": {
                "Contact ID": "P001",
                "Email": schema.UNKNOWN,
                "Source": "https://test.com/careers",
            }
        }
        route, target = resolve_route(outreach_row(), contacts)
        self.assertEqual(route, SendRoute.PORTAL)
        self.assertEqual(target, "https://test.com/careers")

    def test_no_contact_record_is_reported_not_guessed(self):
        route, target = resolve_route(outreach_row(), {})
        self.assertEqual(route, SendRoute.NONE)
        self.assertIn("No contact", target)


class TestExportRespectsApproval(unittest.TestCase):
    """The critical property: manual export cannot bypass the gate."""

    CONTACTS = [{"Contact ID": "P001", "Email": "hr@test.com"}]

    def test_approved_rows_are_exported(self):
        by_email, _ = build_export([outreach_row()], self.CONTACTS, {})
        self.assertEqual(len(by_email), 1)

    def test_pending_rows_are_not_exported(self):
        rows = [outreach_row(**{"Approval Status": schema.APPROVAL_PENDING})]
        by_email, by_portal = build_export(rows, self.CONTACTS, {})
        self.assertEqual(by_email + by_portal, [])

    def test_rejected_rows_are_not_exported(self):
        rows = [outreach_row(**{"Approval Status": schema.APPROVAL_REJECTED})]
        by_email, by_portal = build_export(rows, self.CONTACTS, {})
        self.assertEqual(by_email + by_portal, [])

    def test_already_sent_rows_are_not_exported_again(self):
        rows = [outreach_row(**{"Email Status": schema.EMAIL_SENT})]
        by_email, by_portal = build_export(rows, self.CONTACTS, {})
        self.assertEqual(by_email + by_portal, [])

    def test_a_replied_row_is_not_exported_again(self):
        rows = [outreach_row(**{"Reply Status": "POSITIVE"})]
        by_email, by_portal = build_export(rows, self.CONTACTS, {})
        self.assertEqual(by_email + by_portal, [])

    def test_portal_and_email_rows_are_separated(self):
        contacts = [
            {"Contact ID": "P001", "Email": "hr@test.com"},
            {"Contact ID": "P002", "Email": schema.UNKNOWN, "Source": "https://x.com/jobs"},
        ]
        rows = [
            outreach_row(),
            outreach_row(**{"Outreach ID": "O002", "Contact ID": "P002"}),
        ]
        by_email, by_portal = build_export(rows, contacts, {})
        self.assertEqual(len(by_email), 1)
        self.assertEqual(len(by_portal), 1)


class TestExportFile(unittest.TestCase):
    def test_the_file_contains_the_recipient_and_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            entry = {
                "outreach_id": "O001", "company": "Test Ltd",
                "subject": "Internship enquiry", "body": "Dear Hiring Team,",
                "route": SendRoute.EMAIL, "target": "hr@test.com", "row": 2,
            }
            write_export_file(path, [entry], [], "me@example.com")
            text = path.read_text()

            self.assertIn("hr@test.com", text)
            self.assertIn("Internship enquiry", text)
            self.assertIn("Dear Hiring Team,", text)
            self.assertIn("me@example.com", text)


class TestMarkSent(unittest.TestCase):
    def test_sets_status_and_date(self):
        updates = mark_sent_updates("2026-01-15")
        self.assertEqual(updates["Email Status"], schema.EMAIL_SENT)
        self.assertEqual(updates["Date Sent"], "2026-01-15")

    def test_columns_exist_in_the_schema(self):
        for column in mark_sent_updates("2026-01-15", note="x"):
            self.assertIn(column, schema.OUTREACH_COLUMNS, column)


if __name__ == "__main__":
    unittest.main()


class TestBatchFilters(unittest.TestCase):
    """--only and --since let you send a specific batch, not everything."""

    class Args:
        only = None
        since = None

    ROWS = [
        {"Outreach ID": "O001", "Date Drafted": "2026-09-14"},
        {"Outreach ID": "O016", "Date Drafted": "2026-09-14"},
        {"Outreach ID": "O040", "Date Drafted": "2026-09-15"},
    ]

    def _filter(self, **kwargs):
        import main

        args = self.Args()
        for k, v in kwargs.items():
            setattr(args, k, v)
        return main._filter_rows(self.ROWS, args)

    def test_no_filter_returns_everything(self):
        self.assertEqual(len(self._filter()), 3)

    def test_only_selects_named_ids(self):
        result = self._filter(only=["O016", "O040"])
        self.assertEqual([r["Outreach ID"] for r in result], ["O016", "O040"])

    def test_only_is_case_insensitive(self):
        self.assertEqual(len(self._filter(only=["o016"])), 1)

    def test_since_keeps_that_date_and_later(self):
        result = self._filter(since="2026-09-15")
        self.assertEqual([r["Outreach ID"] for r in result], ["O040"])

    def test_since_includes_the_boundary_date(self):
        self.assertEqual(len(self._filter(since="2026-09-14")), 3)

    def test_an_unmatched_filter_returns_nothing(self):
        self.assertEqual(self._filter(only=["O999"]), [])

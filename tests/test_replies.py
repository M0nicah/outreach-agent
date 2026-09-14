"""Stage 9 tests: reply detection and classification.

The design rule under test: DETECTION never depends on the AI. A
classification failure must still leave you knowing somebody replied,
because missing a reply is far worse than mislabelling one.

Run with:   python -m unittest discover tests
"""

import json
import unittest

from app import schema
from app.replies import (
    FoundReply,
    ReplyClassification,
    build_search_query,
    classify_reply,
    looks_automated,
    match_to_outreach,
    reply_updates,
    trim_quoted_text,
)


def reply(**overrides):
    base = {
        "message_id": "m1",
        "thread_id": "t1",
        "from_address": "hr@test.com",
        "subject": "Re: Internship enquiry",
        "body": "Thanks for writing. Please send your CV.",
        "received": "2026-09-20",
    }
    base.update(overrides)
    return FoundReply(**base)


def outreach_row(**overrides):
    base = {
        "Outreach ID": "O001",
        "Company Name": "Test Ltd",
        "Contact ID": "P001",
        "Subject": "Internship enquiry",
        "Email Status": schema.EMAIL_SENT,
        "Reply Status": schema.REPLY_NONE,
        "Notes": "",
        "_row": 2,
    }
    base.update(overrides)
    return base


class TestSearchQuery(unittest.TestCase):
    def test_builds_a_from_query_for_each_address(self):
        query = build_search_query(["hr@a.com", "jobs@b.com"], days=30)
        self.assertIn("from:hr@a.com", query)
        self.assertIn("from:jobs@b.com", query)
        self.assertIn("newer_than:30d", query)
        self.assertIn("-in:sent", query)

    def test_unknown_addresses_are_excluded(self):
        query = build_search_query([schema.UNKNOWN, "hr@a.com"])
        self.assertNotIn("unknown", query.lower())

    def test_no_addresses_gives_no_query(self):
        self.assertEqual(build_search_query([]), "")


class TestQuotedTextTrimming(unittest.TestCase):
    """A reply quotes your whole email back; classifying that would be wrong."""

    def test_gmail_style_quote_is_removed(self):
        body = (
            "Yes, please send your CV.\n\n"
            "On Mon, 14 Sep 2026 at 09:00, Monica wrote:\n"
            "> I am a BSc Data Science student..."
        )
        trimmed = trim_quoted_text(body)
        self.assertIn("please send your CV", trimmed)
        self.assertNotIn("Data Science student", trimmed)

    def test_outlook_style_quote_is_removed(self):
        body = "We have no openings.\n\n-----Original Message-----\nFrom: Monica"
        self.assertNotIn("Original Message", trim_quoted_text(body))

    def test_a_reply_with_no_quote_is_untouched(self):
        body = "Thank you for your email. We will be in touch."
        self.assertEqual(trim_quoted_text(body), body)


class TestAutomatedDetection(unittest.TestCase):
    def test_bounces_are_detected(self):
        for subject in ["Delivery Status Notification (Failure)", "Undeliverable"]:
            is_bounce, _ = looks_automated(subject, "")
            self.assertTrue(is_bounce, subject)

    def test_out_of_office_is_detected(self):
        _, is_auto = looks_automated("Automatic reply: Internship enquiry", "I am out of office")
        self.assertTrue(is_auto)

    def test_a_real_reply_is_not_flagged(self):
        is_bounce, is_auto = looks_automated(
            "Re: Internship enquiry", "Thanks for reaching out, please send a CV."
        )
        self.assertFalse(is_bounce)
        self.assertFalse(is_auto)


class TestNoAiNeededForAutomated(unittest.TestCase):
    """Bounces and auto-replies must not cost an AI call."""

    def _exploding_ai(self, config, prompt):
        raise AssertionError("the AI must not be called for an automated message")

    def test_a_bounce_is_classified_without_the_ai(self):
        result = classify_reply(
            None, reply(is_bounce=True), outreach_row(), self._exploding_ai
        )
        self.assertEqual(result.classification, "NEGATIVE")
        self.assertIn("different contact", result.next_action)

    def test_an_auto_reply_is_classified_without_the_ai(self):
        result = classify_reply(
            None, reply(is_auto_reply=True), outreach_row(), self._exploding_ai
        )
        self.assertEqual(result.classification, "OTHER")


class TestClassificationValidation(unittest.TestCase):
    def _ai_returning(self, payload):
        def caller(config, prompt):
            return json.dumps(payload)

        return caller

    def test_a_valid_classification_is_accepted(self):
        result = classify_reply(
            None, reply(), outreach_row(),
            self._ai_returning({
                "classification": "CV_REQUEST", "confidence": 0.9,
                "summary": "They asked for a CV.", "next_action": "Send CV",
                "referred_to": schema.UNKNOWN, "deadline_mentioned": schema.UNKNOWN,
            }),
        )
        self.assertEqual(result.classification, "CV_REQUEST")

    def test_an_invented_classification_falls_back_to_other(self):
        """A bad label must not lose the reply."""
        result = classify_reply(
            None, reply(), outreach_row(),
            self._ai_returning({"classification": "MAYBE_LATER", "confidence": 0.9,
                                "summary": "x", "next_action": "y"}),
        )
        self.assertEqual(result.classification, "OTHER")

    def test_no_reply_is_not_a_valid_classification(self):
        """NO_REPLY means nothing came back; it cannot describe a reply."""
        result = classify_reply(
            None, reply(), outreach_row(),
            self._ai_returning({"classification": "NO_REPLY", "confidence": 0.9,
                                "summary": "x", "next_action": "y"}),
        )
        self.assertEqual(result.classification, "OTHER")

    def test_a_broken_ai_still_records_the_reply(self):
        """The key property: detection survives classification failure."""

        def broken(config, prompt):
            raise RuntimeError("API down")

        result = classify_reply(None, reply(), outreach_row(), broken)
        self.assertEqual(result.classification, "OTHER")
        self.assertIn("could not be classified", result.summary)
        self.assertIn("yourself", result.next_action)


class TestMatching(unittest.TestCase):
    CONTACTS = [{"Contact ID": "P001", "Email": "hr@test.com"}]

    def test_a_reply_matches_its_outreach_row(self):
        matched = match_to_outreach([reply()], [outreach_row()], self.CONTACTS)
        self.assertEqual(len(matched), 1)

    def test_an_unmatched_reply_is_not_paired(self):
        matched = match_to_outreach(
            [reply(from_address="stranger@nowhere.com")], [outreach_row()], self.CONTACTS
        )
        self.assertEqual(matched, [])

    def test_unsent_outreach_rows_are_not_matched(self):
        rows = [outreach_row(**{"Email Status": schema.EMAIL_DRAFTED})]
        self.assertEqual(match_to_outreach([reply()], rows, self.CONTACTS), [])


class TestUpdatesStopFollowUps(unittest.TestCase):
    """Recording a reply must stop the follow-up sequence."""

    def test_reply_status_is_written(self):
        result = ReplyClassification(
            classification="POSITIVE", summary="Interested.", next_action="Reply"
        )
        updates = reply_updates(reply(), result)
        self.assertEqual(updates["Reply Status"], "POSITIVE")
        self.assertEqual(updates["Reply Date"], "2026-09-20")

    def test_the_follow_up_sequence_sees_the_reply(self):
        from app.followups import has_replied, next_followup

        result = ReplyClassification(classification="NEGATIVE", summary="No.")
        row = {**outreach_row(), **reply_updates(reply(), result)}

        self.assertTrue(has_replied(row))
        follow_up, reason = next_followup(
            {**row, "Date Sent": "2026-09-14"}, today=__import__("datetime").date(2026, 10, 1)
        )
        self.assertIsNone(follow_up)
        self.assertIn("replied", reason)

    def test_referral_details_go_into_notes(self):
        result = ReplyClassification(
            classification="REFERRAL", summary="Contact HR.",
            referred_to="careers@test.com", deadline_mentioned="30 September",
        )
        updates = reply_updates(reply(), result)
        self.assertIn("careers@test.com", updates["Notes"])
        self.assertIn("30 September", updates["Notes"])

    def test_update_columns_exist_in_the_schema(self):
        result = ReplyClassification(classification="POSITIVE", referred_to="x",
                                     deadline_mentioned="y")
        for column in reply_updates(reply(), result):
            self.assertIn(column, schema.OUTREACH_COLUMNS, column)


if __name__ == "__main__":
    unittest.main()

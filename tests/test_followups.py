"""Stage 10 tests: follow-ups stop when they should.

The two rules that matter more than the dates:

  1. A reply STOPS the sequence immediately, whatever the reply said.
  2. There is never a fourth follow-up.

Run with:   python -m unittest discover tests
"""

import unittest
from datetime import date

from app import schema
from app.followups import (
    MAX_FOLLOWUPS,
    FollowUpError,
    _followup_specific_problems,
    FollowUpDraft,
    find_due,
    followups_sent,
    has_replied,
    next_followup,
    parse_date,
    record_followup_updates,
)

SENT = date(2026, 9, 14)


def row(**overrides):
    base = {
        "Outreach ID": "O001",
        "Company Name": "Test Ltd",
        "Subject": "Internship enquiry",
        "Email Body": "Dear Hiring Team, I am a student and I would like to ask about "
                      "an internship or industrial attachment placement with your team.",
        "Approval Status": schema.APPROVAL_APPROVED,
        "Email Status": schema.EMAIL_SENT,
        "Date Sent": SENT.isoformat(),
        "Reply Status": schema.REPLY_NONE,
        "Follow Up 1": "",
        "Follow Up 2": "",
        "Notes": "",
        "_row": 2,
    }
    base.update(overrides)
    return base


class TestTheStopRule(unittest.TestCase):
    """A reply ends the sequence. This is the most important rule here."""

    def test_every_kind_of_reply_stops_follow_ups(self):
        for status in ["POSITIVE", "NEGATIVE", "REFERRAL", "CV_REQUEST",
                       "INTERVIEW", "OTHER"]:
            follow_up, reason = next_followup(
                row(**{"Reply Status": status}), today=date(2026, 10, 30)
            )
            self.assertIsNone(follow_up, f"{status} did not stop the sequence")
            self.assertIn("replied", reason)

    def test_a_rejection_still_counts_as_a_reply(self):
        """A NEGATIVE reply is still an answer. Do not keep chasing."""
        follow_up, _ = next_followup(
            row(**{"Reply Status": "NEGATIVE"}), today=date(2026, 10, 30)
        )
        self.assertIsNone(follow_up)

    def test_a_reply_date_alone_stops_the_sequence(self):
        """Even with no status set, a reply date means they answered."""
        self.assertTrue(has_replied(row(**{"Reply Date": "2026-09-20"})))

    def test_no_reply_does_not_stop_the_sequence(self):
        follow_up, _ = next_followup(row(), today=date(2026, 9, 19))
        self.assertIsNotNone(follow_up)

    def test_a_blank_reply_status_does_not_stop_the_sequence(self):
        follow_up, _ = next_followup(row(**{"Reply Status": ""}), today=date(2026, 9, 19))
        self.assertIsNotNone(follow_up)


class TestNeverAFourth(unittest.TestCase):
    def test_after_three_follow_ups_the_sequence_is_complete(self):
        done = row(**{
            "Follow Up 1": "2026-09-19",
            "Follow Up 2": "2026-09-26",
            "Notes": "FOLLOWUP3: sent 2026-10-06",
        })
        follow_up, reason = next_followup(done, today=date(2026, 12, 1))
        self.assertIsNone(follow_up)
        self.assertIn("complete", reason)

    def test_counting_follow_ups_already_sent(self):
        self.assertEqual(followups_sent(row()), 0)
        self.assertEqual(followups_sent(row(**{"Follow Up 1": "2026-09-19"})), 1)
        self.assertEqual(
            followups_sent(row(**{"Follow Up 1": "2026-09-19",
                                  "Follow Up 2": "2026-09-26"})), 2
        )
        self.assertEqual(
            followups_sent(row(**{"Follow Up 1": "2026-09-19",
                                  "Follow Up 2": "2026-09-26",
                                  "Notes": "FOLLOWUP3: sent 2026-10-06"})), 3
        )

    def test_there_is_no_follow_up_four(self):
        with self.assertRaises(FollowUpError):
            record_followup_updates(4, "2026-10-10")


class TestSchedule(unittest.TestCase):
    def test_follow_up_one_is_due_on_day_five(self):
        follow_up, _ = next_followup(row(), today=SENT)
        self.assertEqual(follow_up.number, 1)
        self.assertEqual(follow_up.due_date, date(2026, 9, 19))
        self.assertFalse(follow_up.is_due)

        follow_up, _ = next_followup(row(), today=date(2026, 9, 19))
        self.assertTrue(follow_up.is_due)

    def test_follow_up_two_is_due_on_day_twelve(self):
        after_one = row(**{"Follow Up 1": "2026-09-19"})
        follow_up, _ = next_followup(after_one, today=date(2026, 9, 26))
        self.assertEqual(follow_up.number, 2)
        self.assertEqual(follow_up.due_date, date(2026, 9, 26))

    def test_the_final_follow_up_is_labelled_as_final(self):
        after_two = row(**{"Follow Up 1": "2026-09-19", "Follow Up 2": "2026-09-26"})
        follow_up, _ = next_followup(after_two, today=date(2026, 10, 6))
        self.assertEqual(follow_up.number, MAX_FOLLOWUPS)
        self.assertEqual(follow_up.label, "Final follow-up")

    def test_an_unsent_email_has_no_follow_up(self):
        follow_up, reason = next_followup(
            row(**{"Email Status": schema.EMAIL_DRAFTED}), today=date(2026, 10, 1)
        )
        self.assertIsNone(follow_up)
        self.assertEqual(reason, "not sent yet")

    def test_a_missing_sent_date_is_reported_not_guessed(self):
        follow_up, reason = next_followup(row(**{"Date Sent": ""}), today=date(2026, 10, 1))
        self.assertIsNone(follow_up)
        self.assertIn("Date Sent", reason)


class TestDateParsing(unittest.TestCase):
    def test_common_spreadsheet_formats(self):
        self.assertEqual(parse_date("2026-09-14"), date(2026, 9, 14))
        self.assertEqual(parse_date("14/09/2026"), date(2026, 9, 14))

    def test_empty_and_unreadable_values_return_none(self):
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("not a date"))


class TestFindDue(unittest.TestCase):
    def test_rows_are_sorted_into_due_upcoming_and_stopped(self):
        rows = [
            row(**{"Outreach ID": "O001"}),                                  # due
            row(**{"Outreach ID": "O002", "Reply Status": "POSITIVE"}),       # stopped
            row(**{"Outreach ID": "O003", "Email Status": schema.EMAIL_DRAFTED}),  # ignored
        ]
        due, upcoming, stopped = find_due(rows, today=date(2026, 9, 20))

        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].outreach_row["Outreach ID"], "O001")
        self.assertEqual(len(stopped), 1)
        # An unsent draft is not "stopped" -- it is just not in the sequence.
        self.assertNotIn("O003", [r.get("Outreach ID") for r, _ in stopped])


class TestFollowUpQuality(unittest.TestCase):
    def test_resending_the_original_is_caught(self):
        original = row()
        draft = FollowUpDraft(
            subject="Re: Internship enquiry",
            body=original["Email Body"],  # the whole original, resent
        )
        problems = _followup_specific_problems(draft, original)
        self.assertTrue(any("repeats" in p for p in problems), problems)

    def test_guilt_tripping_is_caught(self):
        draft = FollowUpDraft(
            subject="Re: Internship enquiry",
            body="I have not heard back from you about my earlier email, "
                 "and I would still like an answer about the placement.",
        )
        problems = _followup_specific_problems(draft, row())
        self.assertTrue(any("complaint" in p for p in problems), problems)

    def test_an_overlong_follow_up_is_caught(self):
        draft = FollowUpDraft(subject="Re: x", body="word " * 200)
        problems = _followup_specific_problems(draft, row())
        self.assertTrue(any("too long" in p for p in problems), problems)

    def test_a_good_short_follow_up_passes(self):
        draft = FollowUpDraft(
            subject="Re: Internship enquiry",
            body="Dear Hiring Team,\n\nI wrote last week about an industrial "
                 "attachment placement. I wanted to check this reached the right "
                 "person, and I am happy to send a CV if that would help.\n\n"
                 "Thank you for your time.",
        )
        self.assertEqual(_followup_specific_problems(draft, row()), [])


class TestRecording(unittest.TestCase):
    def test_each_follow_up_writes_to_the_right_column(self):
        self.assertIn("Follow Up 1", record_followup_updates(1, "2026-09-19"))
        self.assertIn("Follow Up 2", record_followup_updates(2, "2026-09-26"))
        self.assertIn("Notes", record_followup_updates(3, "2026-10-06"))

    def test_the_third_is_detectable_afterwards(self):
        updates = record_followup_updates(3, "2026-10-06")
        after = row(**{
            "Follow Up 1": "2026-09-19", "Follow Up 2": "2026-09-26", **updates
        })
        self.assertEqual(followups_sent(after), 3)

    def test_columns_exist_in_the_schema(self):
        for number in [1, 2, 3]:
            for column in record_followup_updates(number, "2026-09-19"):
                self.assertIn(column, schema.OUTREACH_COLUMNS, column)


if __name__ == "__main__":
    unittest.main()


class TestSignatureHandling(unittest.TestCase):
    """The signature appears in every email, so it must not trip the checks."""

    SETTINGS = {"Name": "Monica Wughanga Masae"}

    def test_the_signature_is_stripped_from_a_body(self):
        from app.followups import strip_signature

        body = (
            "Dear Hiring Team,\n\nShort follow-up text.\n\n"
            "Monica Wughanga Masae\nBSc Data Science\nm@example.com"
        )
        result = strip_signature(body, self.SETTINGS)
        self.assertIn("Short follow-up text.", result)
        self.assertNotIn("m@example.com", result)

    def test_a_shared_signature_is_not_flagged_as_repetition(self):
        """The real false positive this fixed."""
        signature = "\n\nMonica Wughanga Masae\nBSc Data Science, Open University of Kenya"
        original = row(**{
            "Email Body": "Dear Team,\n\nI am writing to enquire about an internship "
                          "or industrial attachment placement with your team." + signature
        })
        draft = FollowUpDraft(
            subject="Re: Internship enquiry",
            body="Dear Team,\n\nI wrote last week and wanted to check it reached "
                 "the right person." + signature,
        )
        problems = _followup_specific_problems(draft, original, self.SETTINGS)
        self.assertEqual([p for p in problems if "repeats" in p], [])

    def test_genuine_repetition_is_still_caught(self):
        """Stripping signatures must not weaken the real check."""
        original = row()
        draft = FollowUpDraft(
            subject="Re: Internship enquiry", body=original["Email Body"]
        )
        problems = _followup_specific_problems(draft, original, self.SETTINGS)
        self.assertTrue(any("repeats" in p for p in problems), problems)

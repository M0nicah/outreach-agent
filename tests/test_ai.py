"""Tests for the AI provider layer.

None of these make a network call -- they check provider selection and
that errors are translated into messages that say what to do.
"""

import unittest

from app.ai import AIError, _friendly_error, get_ai_caller
from app.config import load_config


class TestProviderSelection(unittest.TestCase):
    def test_mock_is_returned_when_requested(self):
        from app.mock_ai import mock_call

        self.assertIs(get_ai_caller(load_config(), use_mock=True), mock_call)

    def test_unknown_provider_names_the_valid_options(self):
        config = load_config()
        broken = config.__class__(**{**config.__dict__, "ai_provider": "notreal"})
        with self.assertRaises(AIError) as ctx:
            get_ai_caller(broken)
        self.assertIn("gemini", str(ctx.exception))


class TestFriendlyErrors(unittest.TestCase):
    """Provider errors should tell the user what to fix."""

    def test_bad_key_points_at_the_env_file(self):
        message = _friendly_error("Gemini", Exception("API key not valid"))
        self.assertIn("AI_API_KEY", message)

    def test_quota_error_suggests_waiting_or_limiting(self):
        message = _friendly_error("Gemini", Exception("429 quota exceeded"))
        self.assertIn("--limit", message)

    def test_network_error_mentions_the_connection(self):
        message = _friendly_error("Gemini", Exception("Connection timeout"))
        self.assertIn("internet", message)


if __name__ == "__main__":
    unittest.main()

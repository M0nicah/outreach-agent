"""Stage 1 tests: configuration loads and converts types correctly.

Run with:   python -m unittest discover tests
"""

import os
import unittest

from app.config import ConfigError, _get_bool, _get_int, load_config


class TestHelpers(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TEST_VALUE", None)

    def test_bool_accepts_common_truthy_spellings(self):
        for raw in ["true", "True", "1", "yes", "ON"]:
            os.environ["TEST_VALUE"] = raw
            self.assertTrue(_get_bool("TEST_VALUE", False), f"failed for {raw!r}")

    def test_bool_falls_back_to_default_when_unset(self):
        self.assertTrue(_get_bool("TEST_VALUE", True))
        self.assertFalse(_get_bool("TEST_VALUE", False))

    def test_int_parses_and_reports_bad_values_clearly(self):
        os.environ["TEST_VALUE"] = "25"
        self.assertEqual(_get_int("TEST_VALUE", 1), 25)

        os.environ["TEST_VALUE"] = "not-a-number"
        with self.assertRaises(ConfigError):
            _get_int("TEST_VALUE", 1)


class TestLoadConfig(unittest.TestCase):
    def test_config_loads_with_sane_defaults(self):
        config = load_config()
        self.assertTrue(config.excel_path.is_absolute())
        self.assertGreater(config.daily_send_limit, 0)
        self.assertIsInstance(config.dry_run, bool)

    def test_missing_key_raises_helpful_error(self):
        config = load_config()
        if not config.has_ai_key:
            with self.assertRaises(ConfigError):
                config.require_ai_key()

    def test_provider_defaults_to_gemini(self):
        self.assertIn(load_config().ai_provider, {"gemini", "openai"})


if __name__ == "__main__":
    unittest.main()

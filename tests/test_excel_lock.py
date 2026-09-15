"""Tests for the Excel-open guard.

The hazard is specific and easy to miss. On macOS Excel does NOT lock an
open workbook, so Python can write to it successfully. But Excel is still
showing the copy it loaded at open time, and the next Cmd+S writes that
stale copy back -- erasing whatever was written in between.

For most commands that is merely annoying. For `send` it is serious: the
emails have gone out irreversibly, but the record of which ones went is
destroyed.

Run with:   python -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

from app.excel import create_workbook, is_open_in_excel


class TestLockDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "book.xlsx"
        create_workbook(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_closed_workbook_is_not_flagged(self):
        self.assertFalse(is_open_in_excel(self.path))

    def test_excels_lock_file_is_detected(self):
        lock = self.path.parent / f"~${self.path.name}"
        lock.touch()
        self.assertTrue(is_open_in_excel(self.path))

    def test_the_lock_file_is_named_after_that_workbook(self):
        """A lock for a different workbook must not block this one."""
        (self.path.parent / "~$other.xlsx").touch()
        self.assertFalse(is_open_in_excel(self.path))


class TestGuardedCommands(unittest.TestCase):
    """Only commands writing irreplaceable records are blocked."""

    def test_the_guarded_set_covers_sending_and_recording(self):
        import main
        import inspect

        source = inspect.getsource(main.main)
        for command in ["send", "mark-sent", "apply", "check-replies"]:
            self.assertIn(f'"{command}"', source, f"{command} should be guarded")

    def test_read_only_commands_are_not_guarded(self):
        """Browsing with Excel open is harmless and must stay possible."""
        import main
        import inspect

        source = inspect.getsource(main.main)
        guarded = source[source.index("WRITES_IRREPLACEABLE"):source.index("return handler")]
        for command in ["show-companies", "report", "check-approvals", "export"]:
            self.assertNotIn(f'"{command}"', guarded, f"{command} should not be blocked")


if __name__ == "__main__":
    unittest.main()

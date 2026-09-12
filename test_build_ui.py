"""The delivered build fingerprint changes with content, never with the clock."""
import unittest

from build_ui import stamp_ui


class BuildVersionTests(unittest.TestCase):
    html = '<html><head></head><body><script src="/app.js" defer></script></body></html>'

    def test_identical_builds_have_identical_version(self):
        self.assertEqual(stamp_ui(self.html, b"same script"), stamp_ui(self.html, b"same script"))

    def test_script_and_html_changes_each_change_version(self):
        original = stamp_ui(self.html, b"original")[1]
        self.assertNotEqual(original, stamp_ui(self.html, b"changed")[1])
        self.assertNotEqual(original, stamp_ui(self.html.replace("<body>", "<body>New UI"), b"original")[1])

    def test_version_matches_metadata_and_script_cache_key(self):
        stamped, version = stamp_ui(self.html, b"script")
        self.assertEqual(len(version), 64)
        self.assertIn(f'<meta name="football-edits-build" content="{version}">', stamped)
        self.assertIn(f'src="/app.js?v={version}"', stamped)
        self.assertEqual(stamped.count('name="football-edits-build"'), 1)

    def test_missing_entry_point_fails_instead_of_shipping_an_unversioned_app(self):
        with self.assertRaises(ValueError):
            stamp_ui("<html><head></head><body></body></html>", b"script")


if __name__ == "__main__":
    unittest.main()

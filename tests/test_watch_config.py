"""JSON watch config is validated before any fetch."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import watchpage


def write_config(directory: Path, data: dict) -> Path:
    path = directory / "watch.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class WatchConfigTest(unittest.TestCase):
    def assert_parse_error(self, path: Path, expected: str, *, prefix: bool = False):
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                watchpage.load_watch_config(path)
        self.assertEqual(caught.exception.code, 1)
        text = stderr.getvalue()
        if prefix:
            self.assertTrue(text.startswith(expected), text)
            self.assertTrue(text.endswith("\n"))
        else:
            self.assertEqual(text, expected + "\n")
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def base(self, **overrides):
        data = {
            "name": "shop-cart",
            "url": "https://example.com/product",
            "to_numbers": ["+14165550101", "+14165550102"],
            "watch": {
                "kind": "text",
                "value": "add to cart",
                "alert_when": "present",
            },
        }
        data.update(overrides)
        return data

    def test_defaults_message_marker_and_state_file(self):
        path = write_config(self.tmp, self.base())
        config = watchpage.load_watch_config(path)
        self.assertEqual(config["message"], "Change detected: {url}")
        self.assertEqual(config["cron_marker"], "watchpage:shop-cart")
        self.assertEqual(config["state_file"], watchpage.ROOT / "state" / "shop-cart.json")
        self.assertEqual(config["must_contain"], "")
        self.assertEqual(config["page_url"], "https://example.com/product")

    def test_relative_state_file_is_under_the_project(self):
        path = write_config(self.tmp, self.base(state_file="state.json"))
        config = watchpage.load_watch_config(path)
        self.assertEqual(config["state_file"], watchpage.ROOT / "state.json")

    def test_missing_file(self):
        self.assert_parse_error(
            self.tmp / "missing.json", "Parsing error on missing.json: file not found"
        )

    def test_invalid_json(self):
        path = self.tmp / "watch.json"
        path.write_text("{", encoding="utf-8")
        self.assert_parse_error(
            path, "Parsing error on watch.json: invalid JSON", prefix=True
        )

    def test_missing_to_numbers(self):
        data = self.base()
        del data["to_numbers"]
        self.assert_parse_error(
            write_config(self.tmp, data),
            "Parsing error on watch.json: Missing to_numbers key",
        )

    def test_missing_url(self):
        data = self.base()
        del data["url"]
        self.assert_parse_error(
            write_config(self.tmp, data), "Parsing error on watch.json: Missing url key"
        )

    def test_unknown_kind(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "xpath", "value": "//a", "alert_when": "present"}),
        )
        self.assert_parse_error(
            path, 'Parsing error on watch.json: kind must be "text" or "css"'
        )

    def test_unknown_alert_when(self):
        path = write_config(
            self.tmp,
            self.base(
                watch={"kind": "text", "value": "add to cart", "alert_when": "maybe"}
            ),
        )
        self.assert_parse_error(
            path,
            'Parsing error on watch.json: alert_when must be "present" or "absent"',
        )

    def test_missing_alert_when_key(self):
        path = write_config(
            self.tmp,
            self.base(
                watch={"kind": "text", "value": "add to cart", "alertwhen": "absent"}
            ),
        )
        self.assert_parse_error(
            path, "Parsing error on watch.json: Missing alert_when key"
        )

    def test_command_parsing_error_goes_to_stderr(self):
        path = write_config(
            self.tmp,
            self.base(
                watch={"kind": "text", "value": "add to cart", "alertwhen": "absent"}
            ),
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                watchpage.main(["--config", str(path), "--dry-run"])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "Parsing error on watch.json: Missing alert_when key\n",
        )

    def test_empty_value(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "text", "value": "  ", "alert_when": "present"}),
        )
        self.assert_parse_error(path, "Parsing error on watch.json: value is empty")

    def test_invalid_css_selector(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "css", "value": "a[", "alert_when": "present"}),
        )
        self.assert_parse_error(
            path, "Parsing error on watch.json: Invalid CSS selector", prefix=True
        )

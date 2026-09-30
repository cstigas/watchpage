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
        self.assertFalse(config["render_javascript"])
        self.assertIsNone(config["cookies_file"])
        self.assertIsNone(config["user_agent"])

    def test_render_javascript_is_optional(self):
        path = write_config(self.tmp, self.base(render_javascript=True))
        config = watchpage.load_watch_config(path)
        self.assertTrue(config["render_javascript"])

    def test_render_javascript_must_be_a_boolean(self):
        path = write_config(self.tmp, self.base(render_javascript="yes"))
        self.assert_parse_error(
            path, "Parsing error on watch.json: render_javascript must be true or false"
        )

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

    def test_relative_cookies_file_is_under_the_project(self):
        destination = watchpage.ROOT / "cookies" / "unit-test-cookies.txt"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        try:
            path = write_config(self.tmp, self.base(cookies_file="cookies/unit-test-cookies.txt"))
            config = watchpage.load_watch_config(path)
            self.assertEqual(config["cookies_file"], destination)
        finally:
            destination.unlink(missing_ok=True)
            try:
                destination.parent.rmdir()
            except OSError:
                pass

    def test_missing_cookies_file(self):
        path = write_config(self.tmp, self.base(cookies_file="cookies/missing.txt"))
        self.assert_parse_error(
            path,
            "Parsing error on watch.json: cookies file not found: "
            + str(watchpage.ROOT / "cookies" / "missing.txt"),
        )

    def test_import_allows_a_missing_cookies_file(self):
        destination = self.tmp / "jar.txt"
        path = write_config(self.tmp, self.base(cookies_file=str(destination)))
        config = watchpage.load_watch_config(path, require_cookies_file=False)
        self.assertEqual(config["cookies_file"], destination)
        self.assertFalse(destination.exists())

    def test_user_agent(self):
        path = write_config(self.tmp, self.base(user_agent="TicketsBrowser/9"))
        config = watchpage.load_watch_config(path)
        self.assertEqual(config["user_agent"], "TicketsBrowser/9")

    def test_empty_user_agent(self):
        path = write_config(self.tmp, self.base(user_agent="  "))
        self.assert_parse_error(path, "Parsing error on watch.json: user_agent is empty")

    def test_invalid_css_selector(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "css", "value": "a[", "alert_when": "present"}),
        )
        self.assert_parse_error(
            path, "Parsing error on watch.json: Invalid CSS selector", prefix=True
        )

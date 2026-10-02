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
            path, 'Parsing error on watch.json: kind must be "text", "css", or "json"'
        )

    def test_json_watch_reads_check_url_headers_path_and_script(self):
        path = write_config(
            self.tmp,
            self.base(
                check_url="https://api.example.com/stock/42",
                headers={"Accept": "application/json"},
                watch={
                    "kind": "json",
                    "path": "offers.*.availability",
                    "value": "InStock",
                    "script": 'script[type="application/ld+json"]',
                    "alert_when": "present",
                },
            ),
        )
        config = watchpage.load_watch_config(path)
        self.assertEqual(config["fetch_url"], "https://api.example.com/stock/42")
        self.assertEqual(config["page_url"], "https://example.com/product")
        self.assertEqual(config["headers"], {"Accept": "application/json"})
        self.assertEqual(config["watch"]["path"], "offers.*.availability")
        self.assertEqual(config["watch"]["script"], 'script[type="application/ld+json"]')

    def test_check_url_defaults_to_url(self):
        config = watchpage.load_watch_config(write_config(self.tmp, self.base()))
        self.assertEqual(config["fetch_url"], "https://example.com/product")
        self.assertEqual(config["headers"], {})

    def test_json_watch_needs_a_path(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "json", "value": "true", "alert_when": "present"}),
        )
        self.assert_parse_error(path, "Parsing error on watch.json: Missing path key")

    def test_check_url_must_be_http(self):
        path = write_config(self.tmp, self.base(check_url="ftp://example.com/stock"))
        self.assert_parse_error(
            path, "Parsing error on watch.json: check_url must start with http:// or https://"
        )

    def test_headers_must_be_text(self):
        path = write_config(self.tmp, self.base(headers={"X-Count": 3}))
        self.assert_parse_error(
            path, "Parsing error on watch.json: headers must be an object of text values"
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

    def test_copied_config_reports_shared_name_marker_and_state(self):
        original = self.tmp / "config.instore.json"
        original.write_text(json.dumps(self.base(name="instore")), encoding="utf-8")
        copy = self.tmp / "config.instore-copy.json"
        copy.write_text(json.dumps(self.base(name="instore")), encoding="utf-8")
        (self.tmp / "notes.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(
            watchpage.watch_conflicts(copy),
            [
                "name instore is also used by config.instore.json",
                "cron_marker watchpage:instore is also used by config.instore.json",
                "state_file state/instore.json is also used by config.instore.json",
            ],
        )

    def test_distinct_configs_have_no_conflicts(self):
        first = self.tmp / "config.one.json"
        first.write_text(json.dumps(self.base(name="one")), encoding="utf-8")
        second = self.tmp / "config.two.json"
        second.write_text(json.dumps(self.base(name="two")), encoding="utf-8")
        self.assertEqual(watchpage.watch_conflicts(second), [])

    def test_invalid_css_selector(self):
        path = write_config(
            self.tmp,
            self.base(watch={"kind": "css", "value": "a[", "alert_when": "present"}),
        )
        self.assert_parse_error(
            path, "Parsing error on watch.json: Invalid CSS selector", prefix=True
        )

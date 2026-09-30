"""Dry run reports the watch and does not alert or write state."""

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

import watchpage


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "state.json"
        self.config_path = self.tmp / "watch.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "name": "christmas-town",
                    "url": "https://www.conservationhalton.ca/christmas-town/",
                    "message": "Christmas Town tickets may be on sale: {url}",
                    "to_numbers": ["+14165550101"],
                    "must_contain": "christmas town",
                    "state_file": str(self.state_path),
                    "watch": {
                        "kind": "text",
                        "value": "will be available",
                        "alert_when": "absent",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.saved_env = {key: os.environ.get(key) for key in watchpage.REQUIRED_SETTINGS}
        for key in watchpage.REQUIRED_SETTINGS:
            os.environ.pop(key, None)
        self.saved_env_path = watchpage.ENV_PATH
        self.saved_state_path = watchpage.STATE_PATH
        watchpage.ENV_PATH = self.tmp / "missing.env"
        self.original_fetch = watchpage.fetch_page
        self.original_sms = watchpage.send_sms
        self.original_run = watchpage.subprocess.run
        self.page = None
        self.problem = "fetch failed"

        def fake_fetch(_url):
            if self.page is None:
                return None, self.problem
            return self.page, None

        def reject_sms(*_args, **_kwargs):
            raise AssertionError("dry run must not send SMS")

        def reject_cron(*_args, **_kwargs):
            raise AssertionError("dry run must not edit crontab")

        watchpage.fetch_page = fake_fetch
        watchpage.send_sms = reject_sms
        watchpage.subprocess.run = reject_cron

    def tearDown(self):
        watchpage.ENV_PATH = self.saved_env_path
        watchpage.STATE_PATH = self.saved_state_path
        watchpage.fetch_page = self.original_fetch
        watchpage.send_sms = self.original_sms
        watchpage.subprocess.run = self.original_run
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_dry(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = watchpage.main(["--config", str(self.config_path), "--dry-run"])
        self.assertFalse(self.state_path.exists())
        return code, stdout.getvalue()

    def test_triggered(self):
        self.page = "Christmas Town tickets are on sale now. " * 20
        code, output = self.run_dry()
        self.assertEqual(code, 0)
        self.assertIn("watch triggered", output)
        self.assertNotIn("watch not triggered", output)

    def test_not_triggered(self):
        self.page = "Christmas Town tickets will be available. " * 20
        code, output = self.run_dry()
        self.assertEqual(code, 0)
        self.assertIn("watch not triggered", output)

    def test_not_checked_when_fetch_fails(self):
        code, output = self.run_dry()
        self.assertEqual(code, 1)
        self.assertIn("watch not checked: fetch failed", output)

    def test_not_checked_when_page_is_too_short(self):
        self.problem = "page too short"
        code, output = self.run_dry()
        self.assertEqual(code, 1)
        self.assertIn("watch not checked: page too short", output)

    def test_not_checked_when_expected_text_is_missing(self):
        self.page = "A parking page with no event name. " * 20
        code, output = self.run_dry()
        self.assertEqual(code, 1)
        self.assertIn(
            "watch not checked: page did not contain the expected text", output
        )

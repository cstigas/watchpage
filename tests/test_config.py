"""Required settings produce a warning when they are missing."""

import os
import tempfile
import unittest
from pathlib import Path

import watch


class ConfigWarningTest(unittest.TestCase):
    def setUp(self):
        watch.warn_config()
        self.saved = {key: os.environ.get(key) for key in watch.REQUIRED_SETTINGS}
        for key in watch.REQUIRED_SETTINGS:
            os.environ.pop(key, None)
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_env(self, outage: str) -> Path:
        path = self.tmp / ".env"
        path.write_text(
            "\n".join(
                [
                    "TWILIO_ACCOUNT_SID=ACtest",
                    "TWILIO_AUTH_TOKEN=secret",
                    "TWILIO_FROM_NUMBER=+14165550100",
                    "TO_NUMBERS=+14165550101,+14165550102",
                    f"OUTAGE_TO_NUMBER={outage}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_complete_config_has_no_warnings(self):
        path = self.write_env("+14165550199")
        self.assertEqual(watch.config_warnings(path), [])

    def test_blank_outage_number_warns(self):
        path = self.write_env("")
        self.assertIn("OUTAGE_TO_NUMBER is not set", watch.config_warnings(path))

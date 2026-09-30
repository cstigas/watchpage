"""Outage texts go to OUTAGE_TO_NUMBER once per stretch of failures."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import watch

ENV_KEYS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "TO_NUMBERS",
    "OUTAGE_TO_NUMBER",
)


class OutageAlertTest(unittest.TestCase):
    def setUp(self):
        watch.warn_config()
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.saved_test_mode = os.environ.get("DONT_UPDATE_STATE")
        os.environ["DONT_UPDATE_STATE"] = "1"
        self.real_state = watch.STATE_PATH
        self.real_state_existed = self.real_state.exists()
        self.real_state_bytes = (
            self.real_state.read_bytes() if self.real_state_existed else None
        )
        self.saved_paths = (watch.ENV_PATH, watch.STATE_PATH)
        self.original_run = watch.subprocess.run
        self.original_load_state = watch.load_state
        self.original_save_state = watch.save_state
        self.stored = None
        watch.ENV_PATH = self.env_path

        def load_state():
            if self.stored is None:
                return watch.empty_state()
            return json.loads(json.dumps(self.stored))

        def save_state(state):
            self.stored = json.loads(json.dumps(state))

        watch.load_state = load_state
        watch.save_state = save_state
        self.sent = []
        self.page = None

        def fake_fetch(_url):
            if self.page is None:
                return None, "fetch failed"
            return self.page, None

        def fake_sms(_config, number, body):
            self.sent.append((number, body))
            return True

        self.original_fetch = watch.fetch_page
        self.original_sms = watch.send_sms
        watch.fetch_page = fake_fetch
        watch.send_sms = fake_sms
        self.write_env(outage="+14165550199")
        self.config_path = self.tmp / "watch.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "name": "christmas-town",
                    "url": "https://www.conservationhalton.ca/christmas-town/",
                    "message": "Christmas Town tickets may be on sale: {url}",
                    "must_contain": "christmas town",
                    "cron_marker": "christmas-town-watch",
                    "state_file": str(self.tmp / "state.json"),
                    "watch": {
                        "kind": "text",
                        "value": "will be available",
                        "alert_when": "absent",
                    },
                }
            ),
            encoding="utf-8",
        )

    def run_watch(self, extra=()):
        return watch.main(["--config", str(self.config_path), *extra])

    def tearDown(self):
        watch.ENV_PATH, watch.STATE_PATH = self.saved_paths
        watch.fetch_page = self.original_fetch
        watch.send_sms = self.original_sms
        watch.subprocess.run = self.original_run
        watch.load_state = self.original_load_state
        watch.save_state = self.original_save_state
        if self.real_state_existed:
            self.assertEqual(self.real_state.read_bytes(), self.real_state_bytes)
        else:
            self.assertFalse(
                self.real_state.exists(),
                "tests must not create the project state.json",
            )
        if self.saved_test_mode is None:
            os.environ.pop("DONT_UPDATE_STATE", None)
        else:
            os.environ["DONT_UPDATE_STATE"] = self.saved_test_mode
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_env(self, outage: str):
        self.env_path.write_text(
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

    def test_one_text_after_ten_failures_and_none_after_recovery(self):
        for _ in range(watch.OUTAGE_FAILURE_THRESHOLD - 1):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(self.sent, [])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        number, body = self.sent[0]
        self.assertEqual(number, "+14165550199")
        self.assertEqual(
            body,
            "christmas-town page unreachable for 10 checks: "
            "https://www.conservationhalton.ca/christmas-town/",
        )
        self.assertEqual(self.stored["sent_to"], [])
        self.assertTrue(self.stored["outage_alerted"])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)

        self.page = "Christmas Town tickets will be available. " * 20
        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.stored["consecutive_failures"], 0)
        self.assertFalse(self.stored["outage_alerted"])

        self.page = None
        self.sent.clear()
        for _ in range(watch.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "+14165550199")

    def test_blank_outage_number_does_not_text_sale_recipients(self):
        self.write_env(outage="")
        for _ in range(watch.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stored["consecutive_failures"], watch.OUTAGE_FAILURE_THRESHOLD)
        self.assertFalse(self.stored["outage_alerted"])

    def test_failed_outage_text_retries_on_the_next_check(self):
        def fail_once(_config, number, body):
            self.sent.append((number, body))
            return len(self.sent) > 1

        watch.send_sms = fail_once
        for _ in range(watch.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertFalse(self.stored["outage_alerted"])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.stored["outage_alerted"])
        self.assertTrue(
            self.sent[1][1].startswith("christmas-town page unreachable for 11 checks:")
        )

    def test_no_record_does_not_write_state_or_edit_cron(self):
        def reject_cron(*_args, **_kwargs):
            raise AssertionError("crontab must not be edited")

        watch.subprocess.run = reject_cron
        self.page = "Christmas Town tickets are on sale now. " * 20
        self.assertEqual(self.run_watch(["--no-record"]), 0)
        self.assertIsNone(self.stored)
        self.assertEqual([number for number, _body in self.sent], ["+14165550101", "+14165550102"])


class HungServerOutageTest(unittest.TestCase):
    def setUp(self):
        watch.warn_config()
        self.original_threshold = watch.OUTAGE_FAILURE_THRESHOLD
        self.original_sms = watch.send_sms
        self.original_timeout = watch.FETCH_TIMEOUT_SECONDS
        self.real_state = watch.STATE_PATH
        self.real_state_existed = self.real_state.exists()
        self.real_state_bytes = (
            self.real_state.read_bytes() if self.real_state_existed else None
        )
        self.sent = []
        watch.OUTAGE_FAILURE_THRESHOLD = 1

        def fake_sms(_config, number, body):
            self.sent.append((number, body))
            return True

        watch.send_sms = fake_sms

    def tearDown(self):
        watch.OUTAGE_FAILURE_THRESHOLD = self.original_threshold
        watch.send_sms = self.original_sms
        watch.FETCH_TIMEOUT_SECONDS = self.original_timeout
        if self.real_state_existed:
            self.assertEqual(self.real_state.read_bytes(), self.real_state_bytes)
        else:
            self.assertFalse(self.real_state.exists())

    def test_flag_does_not_load_a_watch_config(self):
        original_load = watch.load_config
        original_run = watch.run_outage_test
        original_watch = watch.load_watch_config

        def reject_watch(_path):
            raise AssertionError("watch config must not be loaded")

        watch.load_watch_config = reject_watch
        watch.load_config = lambda: {"outage_number": "+14165550199"}
        watch.run_outage_test = lambda _config: 0
        try:
            self.assertEqual(watch.main(["--test-outage"]), 0)
        finally:
            watch.load_config = original_load
            watch.run_outage_test = original_run
            watch.load_watch_config = original_watch

    def test_hung_server_times_out_and_sends_one_text(self):
        code = watch.run_outage_test({"outage_number": "+14165550199"})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        number, body = self.sent[0]
        self.assertEqual(number, "+14165550199")
        self.assertIn("http://127.0.0.1:", body)
        self.assertTrue(body.startswith("Outage test: unreachable for 1 checks:"))

"""Outage texts go to OUTAGE_TO_NUMBER once per stretch of failures."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import watchpage

ENV_KEYS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "OUTAGE_TO_NUMBER",
)


class OutageAlertTest(unittest.TestCase):
    def setUp(self):
        watchpage.warn_config()
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.saved_test_mode = os.environ.get("DONT_UPDATE_STATE")
        os.environ["DONT_UPDATE_STATE"] = "1"
        self.real_state = watchpage.STATE_PATH
        self.real_state_existed = self.real_state.exists()
        self.real_state_bytes = (
            self.real_state.read_bytes() if self.real_state_existed else None
        )
        self.saved_paths = (watchpage.ENV_PATH, watchpage.STATE_PATH)
        self.original_run = watchpage.subprocess.run
        self.original_load_state = watchpage.load_state
        self.original_save_state = watchpage.save_state
        self.stored = None
        watchpage.ENV_PATH = self.env_path

        def load_state():
            if self.stored is None:
                return watchpage.empty_state()
            return json.loads(json.dumps(self.stored))

        def save_state(state):
            self.stored = json.loads(json.dumps(state))

        watchpage.load_state = load_state
        watchpage.save_state = save_state
        self.sent = []
        self.page = None

        def fake_fetch(_url, **_kwargs):
            if self.page is None:
                return None, "fetch failed"
            return self.page, None

        def fake_sms(_config, number, body):
            self.sent.append((number, body))
            return True

        self.original_fetch = watchpage.fetch_page
        self.original_sms = watchpage.send_sms
        watchpage.fetch_page = fake_fetch
        watchpage.send_sms = fake_sms
        self.write_env(outage="+14165550199")
        self.config_path = self.tmp / "watch.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "name": "summer-tickets",
                    "url": "https://example.com/tickets",
                    "message": "Tickets may be on sale: {url}",
                    "to_numbers": ["+14165550101", "+14165550102"],
                    "must_contain": "summer concert",
                    "cron_marker": "watchpage:summer-tickets",
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
        return watchpage.main(["--config", str(self.config_path), *extra])

    def tearDown(self):
        watchpage.ENV_PATH, watchpage.STATE_PATH = self.saved_paths
        watchpage.fetch_page = self.original_fetch
        watchpage.send_sms = self.original_sms
        watchpage.subprocess.run = self.original_run
        watchpage.load_state = self.original_load_state
        watchpage.save_state = self.original_save_state
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
                    f"OUTAGE_TO_NUMBER={outage}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_one_text_after_ten_failures_and_none_after_recovery(self):
        for _ in range(watchpage.OUTAGE_FAILURE_THRESHOLD - 1):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(self.sent, [])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        number, body = self.sent[0]
        self.assertEqual(number, "+14165550199")
        self.assertEqual(
            body,
            "summer-tickets page unreachable for 10 checks: "
            "https://example.com/tickets",
        )
        self.assertEqual(self.stored["sent_to"], [])
        self.assertTrue(self.stored["outage_alerted"])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)

        self.page = "Summer concert tickets will be available. " * 20
        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.stored["consecutive_failures"], 0)
        self.assertFalse(self.stored["outage_alerted"])

        self.page = None
        self.sent.clear()
        for _ in range(watchpage.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "+14165550199")

    def test_blank_outage_number_does_not_text_sale_recipients(self):
        self.write_env(outage="")
        for _ in range(watchpage.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.stored["consecutive_failures"], watchpage.OUTAGE_FAILURE_THRESHOLD)
        self.assertFalse(self.stored["outage_alerted"])

    def test_failed_outage_text_retries_on_the_next_check(self):
        def fail_once(_config, number, body):
            self.sent.append((number, body))
            return len(self.sent) > 1

        watchpage.send_sms = fail_once
        for _ in range(watchpage.OUTAGE_FAILURE_THRESHOLD):
            self.assertEqual(self.run_watch(), 0)
        self.assertFalse(self.stored["outage_alerted"])

        self.assertEqual(self.run_watch(), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.stored["outage_alerted"])
        self.assertTrue(
            self.sent[1][1].startswith("summer-tickets page unreachable for 11 checks:")
        )

    def test_no_record_does_not_write_state_or_edit_cron(self):
        def reject_cron(*_args, **_kwargs):
            raise AssertionError("crontab must not be edited")

        watchpage.subprocess.run = reject_cron
        self.page = "Summer concert tickets are on sale now. " * 20
        self.assertEqual(self.run_watch(["--no-record"]), 0)
        self.assertIsNone(self.stored)
        self.assertEqual([number for number, _body in self.sent], ["+14165550101", "+14165550102"])


class HungServerOutageTest(unittest.TestCase):
    def setUp(self):
        watchpage.warn_config()
        self.original_threshold = watchpage.OUTAGE_FAILURE_THRESHOLD
        self.original_sms = watchpage.send_sms
        self.original_timeout = watchpage.FETCH_TIMEOUT_SECONDS
        self.real_state = watchpage.STATE_PATH
        self.real_state_existed = self.real_state.exists()
        self.real_state_bytes = (
            self.real_state.read_bytes() if self.real_state_existed else None
        )
        self.sent = []
        watchpage.OUTAGE_FAILURE_THRESHOLD = 1

        def fake_sms(_config, number, body):
            self.sent.append((number, body))
            return True

        watchpage.send_sms = fake_sms

    def tearDown(self):
        watchpage.OUTAGE_FAILURE_THRESHOLD = self.original_threshold
        watchpage.send_sms = self.original_sms
        watchpage.FETCH_TIMEOUT_SECONDS = self.original_timeout
        if self.real_state_existed:
            self.assertEqual(self.real_state.read_bytes(), self.real_state_bytes)
        else:
            self.assertFalse(self.real_state.exists())

    def test_flag_does_not_load_a_watch_config(self):
        original_load = watchpage.load_config
        original_run = watchpage.run_outage_test
        original_watch = watchpage.load_watch_config

        def reject_watch(_path):
            raise AssertionError("watch config must not be loaded")

        watchpage.load_watch_config = reject_watch
        watchpage.load_config = lambda: {"outage_number": "+14165550199"}
        watchpage.run_outage_test = lambda _config: 0
        try:
            self.assertEqual(watchpage.main(["--test-outage"]), 0)
        finally:
            watchpage.load_config = original_load
            watchpage.run_outage_test = original_run
            watchpage.load_watch_config = original_watch

    def test_hung_server_times_out_and_sends_one_text(self):
        code = watchpage.run_outage_test({"outage_number": "+14165550199"})
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        number, body = self.sent[0]
        self.assertEqual(number, "+14165550199")
        self.assertIn("http://127.0.0.1:", body)
        self.assertTrue(body.startswith("Outage test: unreachable for 1 checks:"))

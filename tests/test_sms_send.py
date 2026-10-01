"""Send a real test text to each number in config.json.

The full suite skips this. ./run_tests.sh --test test_sms_send opts in.
"""

import os
import unittest

import watchpage

TEST_MESSAGE = "testing sms send functionality"


@unittest.skipUnless(
    os.environ.get("SEND_TEST_SMS") == "1",
    "sends a real SMS; run ./run_tests.sh --test test_sms_send",
)
class SendSmsTest(unittest.TestCase):
    def setUp(self):
        watchpage.warn_config()

    def test_sends_to_each_configured_number(self):
        config = watchpage.load_config()
        config.update(watchpage.load_watch_config(watchpage.ROOT / "config.json"))
        recipients = config["recipients"]
        self.assertTrue(recipients, "to_numbers is empty")
        for number in recipients:
            sent = watchpage.send_sms(config, number, TEST_MESSAGE)
            self.assertTrue(sent, f"Twilio did not accept the message to {number}")


if __name__ == "__main__":
    unittest.main()

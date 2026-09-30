"""Send a real test text to each number in .env."""

import unittest

import watch

TEST_MESSAGE = "testing sms send functionality"


class SendSmsTest(unittest.TestCase):
    def setUp(self):
        watch.warn_config()

    def test_sends_to_each_configured_number(self):
        config = watch.load_config()
        recipients = config["recipients"]
        self.assertTrue(recipients, "TO_NUMBERS is empty")
        for number in recipients:
            sent = watch.send_sms(config, number, TEST_MESSAGE)
            self.assertTrue(sent, f"Twilio did not accept the message to {number}")


if __name__ == "__main__":
    unittest.main()

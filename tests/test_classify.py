"""Page classification for text and CSS watches."""

import unittest

import watch


def watch_config(kind, value, alert_when, must_contain=""):
    return {
        "must_contain": must_contain,
        "watch": {"kind": kind, "value": value, "alert_when": alert_when},
    }


class ClassifyPageTest(unittest.TestCase):
    def test_text_absent_waits_while_phrase_remains(self):
        body = "Christmas Town tickets will be available late September."
        config = watch_config("text", "will be available", "absent", "christmas town")
        self.assertEqual(watch.classify_page(body, config), "waiting")

    def test_text_absent_triggers_when_phrase_is_gone(self):
        body = "Christmas Town tickets are on sale now."
        config = watch_config("text", "will be available", "absent", "christmas town")
        self.assertEqual(watch.classify_page(body, config), "triggered")

    def test_text_present_triggers_when_phrase_shows_up(self):
        body = "Please Add To Cart to reserve a ticket."
        config = watch_config("text", "add to cart", "present")
        self.assertEqual(watch.classify_page(body, config), "triggered")

    def test_text_present_waits_when_phrase_is_missing(self):
        body = "Sold out for the season."
        config = watch_config("text", "add to cart", "present")
        self.assertEqual(watch.classify_page(body, config), "waiting")

    def test_css_present_triggers_when_selector_matches(self):
        body = '<html><body><a class="buy-button" href="/buy">Buy</a></body></html>'
        config = watch_config("css", "a.buy-button", "present")
        self.assertEqual(watch.classify_page(body, config), "triggered")

    def test_css_present_waits_when_selector_does_not_match(self):
        body = "<html><body><p>Not yet</p></body></html>"
        config = watch_config("css", "a.buy-button", "present")
        self.assertEqual(watch.classify_page(body, config), "waiting")

    def test_css_absent_waits_while_selector_matches(self):
        body = '<html><body><p class="sold-out">Sold out</p></body></html>'
        config = watch_config("css", ".sold-out", "absent")
        self.assertEqual(watch.classify_page(body, config), "waiting")

    def test_css_absent_triggers_when_selector_is_gone(self):
        body = "<html><body><p>Tickets are open</p></body></html>"
        config = watch_config("css", ".sold-out", "absent")
        self.assertEqual(watch.classify_page(body, config), "triggered")

    def test_must_contain_skips_a_page_that_lacks_the_expected_text(self):
        body = "This parking page does not mention the event, and will be available is gone."
        config = watch_config("text", "will be available", "absent", "christmas town")
        self.assertEqual(watch.classify_page(body, config), "unexpected")

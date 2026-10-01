"""Page classification for text and CSS watches."""

import unittest

import watchpage


def watch_config(kind, value, alert_when, must_contain=""):
    return {
        "must_contain": must_contain,
        "watch": {"kind": kind, "value": value, "alert_when": alert_when},
    }


class ClassifyPageTest(unittest.TestCase):
    def test_text_absent_waits_while_phrase_remains(self):
        body = "Summer concert tickets will be available late September."
        config = watch_config("text", "will be available", "absent", "summer concert")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_text_absent_triggers_when_phrase_is_gone(self):
        body = "Summer concert tickets are on sale now."
        config = watch_config("text", "will be available", "absent", "summer concert")
        self.assertEqual(watchpage.classify_page(body, config), "triggered")

    def test_text_present_triggers_when_phrase_shows_up(self):
        body = "Please Add To Cart to reserve a ticket."
        config = watch_config("text", "add to cart", "present")
        self.assertEqual(watchpage.classify_page(body, config), "triggered")

    def test_text_present_waits_when_phrase_is_missing(self):
        body = "Sold out for the season."
        config = watch_config("text", "add to cart", "present")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_css_present_triggers_when_selector_matches(self):
        body = '<html><body><a class="buy-button" href="/buy">Buy</a></body></html>'
        config = watch_config("css", "a.buy-button", "present")
        self.assertEqual(watchpage.classify_page(body, config), "triggered")

    def test_css_present_waits_when_selector_does_not_match(self):
        body = "<html><body><p>Not yet</p></body></html>"
        config = watch_config("css", "a.buy-button", "present")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_css_absent_waits_while_selector_matches(self):
        body = '<html><body><p class="sold-out">Sold out</p></body></html>'
        config = watch_config("css", ".sold-out", "absent")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_css_absent_triggers_when_selector_is_gone(self):
        body = "<html><body><p>Tickets are open</p></body></html>"
        config = watch_config("css", ".sold-out", "absent")
        self.assertEqual(watchpage.classify_page(body, config), "triggered")

    def test_must_contain_skips_a_page_that_lacks_the_expected_text(self):
        body = "This parking page does not mention the event, and will be available is gone."
        config = watch_config("text", "will be available", "absent", "summer concert")
        self.assertEqual(watchpage.classify_page(body, config), "unexpected")


def json_config(path, value, alert_when="present", script=None, must_contain=""):
    watch = {"kind": "json", "path": path, "value": value, "alert_when": alert_when}
    if script:
        watch["script"] = script
    return {"must_contain": must_contain, "watch": watch}


class ClassifyJsonTest(unittest.TestCase):
    STOCK = '{"isAvailable": true, "itemNumber": "2022293", "count": 3}'

    def test_true_matches_json_true(self):
        config = json_config("isAvailable", "true", must_contain="2022293")
        self.assertEqual(watchpage.classify_page(self.STOCK, config), "triggered")

    def test_false_waits(self):
        body = '{"isAvailable": false, "itemNumber": "2600511"}'
        config = json_config("isAvailable", "true", must_contain="2600511")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_numbers_and_absent(self):
        self.assertEqual(
            watchpage.classify_page(self.STOCK, json_config("count", "0", "absent")),
            "triggered",
        )

    def test_missing_path_waits_for_a_present_watch(self):
        body = '{"itemNumber": "2600511", "warehouseAvailability": {}}'
        config = json_config("warehouseAvailability.inWarehouse.availability", "INSTOCK")
        self.assertEqual(watchpage.classify_page(body, config), "waiting")

    def test_missing_path_never_alerts_an_absent_watch(self):
        config = json_config("stock.status", "sold out", "absent")
        self.assertEqual(watchpage.classify_page(self.STOCK, config), "no_value")

    def test_body_that_is_not_json_is_not_a_result(self):
        config = json_config("isAvailable", "true")
        self.assertEqual(watchpage.classify_page("<html>Access denied</html>", config), "no_value")

    def test_list_index_and_wildcard(self):
        body = '{"items": [{"status": "OUT"}, {"status": "IN_STOCK"}]}'
        self.assertEqual(
            watchpage.classify_page(body, json_config("items.1.status", "in_stock")),
            "triggered",
        )
        self.assertEqual(
            watchpage.classify_page(body, json_config("items.*.status", "IN_STOCK")),
            "triggered",
        )
        self.assertEqual(
            watchpage.classify_page(body, json_config("items.0.status", "IN_STOCK")),
            "waiting",
        )

    def test_json_embedded_in_a_script_tag(self):
        body = (
            "<html><head>"
            '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
            '<script type="application/ld+json">'
            '{"@type": "Product", "offers": {"availability": "https://schema.org/InStock"}}'
            "</script></head><body>Product</body></html>"
        )
        config = json_config(
            "offers.availability",
            "https://schema.org/InStock",
            script='script[type="application/ld+json"]',
        )
        self.assertEqual(watchpage.classify_page(body, config), "triggered")

    def test_assess_names_the_missing_path(self):
        config = json_config("stock.status", "true", "absent")
        self.assertEqual(
            watchpage.assess(self.STOCK, None, config),
            ("not_checked", "no JSON value at stock.status"),
        )

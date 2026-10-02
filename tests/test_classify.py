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

    def test_reply_error_reads_a_non_empty_error_field(self):
        watch = {"kind": "json", "path": "isAvailable", "value": "true", "error_path": "errorParam"}
        rejected = '{"isAvailable": false, "errorParam": {"Reason": "Invalid Client ID received"}}'
        self.assertEqual(
            watchpage.reply_error(rejected, watch),
            '{"Reason": "Invalid Client ID received"}',
        )
        for body in ('{"isAvailable": true, "errorParam": {}}', '{"isAvailable": true}'):
            self.assertIsNone(watchpage.reply_error(body, watch))
        self.assertIsNone(watchpage.reply_error(rejected, {**watch, "error_path": ""}))

    def test_assess_names_the_missing_path(self):
        config = json_config("stock.status", "true", "absent")
        self.assertEqual(
            watchpage.assess(self.STOCK, None, config),
            ("not_checked", "no JSON value at stock.status"),
        )

    def test_verbose_detail_explains_a_json_miss(self):
        body = '{"isAvailable": false, "itemNumber": "2600511"}'
        config = json_config("isAvailable", "true", must_contain="2600511")
        self.assertEqual(
            watchpage.watch_detail(body, config),
            [
                'looking for JSON isAvailable to be "true"',
                'isAvailable is "false", not "true", so the alert does not match',
            ],
        )

    def test_verbose_detail_explains_a_json_match_and_a_missing_path(self):
        config = json_config("isAvailable", "true", must_contain="2022293")
        self.assertEqual(
            watchpage.watch_detail(self.STOCK, config)[1],
            'isAvailable is "true", so the alert matches',
        )
        missing = json_config(
            "warehouseAvailability.inWarehouse.availability", "INSTOCK"
        )
        body = '{"itemNumber": "2600511", "warehouseAvailability": {}}'
        self.assertEqual(
            watchpage.watch_detail(body, missing)[1],
            "nothing at warehouseAvailability.inWarehouse.availability, "
            "so the alert does not match",
        )
        absent = json_config("stock.status", "sold out", "absent")
        self.assertEqual(
            watchpage.watch_detail(self.STOCK, absent)[1],
            "nothing at stock.status, so an absent alert was not checked",
        )

    def test_verbose_detail_explains_text_css_and_must_contain(self):
        waiting = watch_config("text", "add to cart", "present")
        self.assertEqual(
            watchpage.watch_detail("Sold out for the season.", waiting),
            [
                'looking for text "add to cart" to be present',
                'page does not contain "add to cart", so the alert does not match',
            ],
        )
        css = watch_config("css", ".sold-out", "absent")
        body = '<html><body><p class="sold-out">Sold out</p></body></html>'
        self.assertEqual(
            watchpage.watch_detail(body, css)[1],
            'selector ".sold-out" matched 1 element, so the alert does not match',
        )
        gated = watch_config("text", "will be available", "absent", "summer concert")
        page = "This parking page does not mention the event."
        self.assertEqual(
            watchpage.watch_detail(page, gated)[1],
            'page does not contain "summer concert", so the alert was not checked',
        )
        self.assertEqual(
            watchpage.watch_detail(None, waiting),
            ['looking for text "add to cart" to be present'],
        )

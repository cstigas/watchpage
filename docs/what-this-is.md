# What this is

watchpage is a small HTTP watcher. You give it a page URL and something to watch for. Cron runs it once a minute. When the watch condition is met, it sends one Twilio SMS to each number in that config's `to_numbers`, records the send, and comments out its own cron line.

The watch lives in a JSON file passed as `--config`. Twilio credentials stay in `.env`.

## What it watches

`watch.kind` is `text` or `css`. `watch.alert_when` is `present` or `absent`.

- Text, absent: text when a phrase is gone. The Christmas Town config waits while the page says `will be available`, then texts `Christmas Town tickets may be on sale: {url}`.
- Text, present: text when a phrase shows up, such as `add to cart`.
- CSS, present: text when a selector such as `a.buy-button` matches.
- CSS, absent: text when a selector such as `.sold-out` no longer matches.

`must_contain` is optional. A page that lacks that text is skipped, so a wrong page does not count as the phrase being gone.

`--dry-run` fetches the page and prints `watch triggered`, `watch not triggered`, or `watch not checked`. It does not send a text or change state.

Each successful send is stored in the state file named by the config. A retry texts only numbers that have not been recorded. Once every number has been sent the alert, the script comments out the crontab line whose comment matches `cron_marker`. If it is ever started again, it exits before fetching the site.

If the page fails to respond 10 times in a row, one text goes to `OUTAGE_TO_NUMBER`. More failures stay quiet until a check succeeds. That count is `consecutive_failures` in the state file. An outage text does not count as the watch alert, and it does not stop the watcher.

Setup steps are in the project README.

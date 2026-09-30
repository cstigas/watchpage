# watchpage

A Python 3 script for an Ubuntu server. Once a minute it fetches a page from a JSON config. While the watch condition is not met, it waits. When the condition is met, it sends one Twilio SMS to each configured number, records the send, and comments out its own cron job. Later runs exit immediately and do not fetch the site again.

Text watches use the Python standard library only. A CSS watch needs BeautifulSoup. `./setup.sh` creates a `.venv` in this directory and installs whatever is missing from `requirements.txt`. It does not need root.

```bash
./setup.sh
```

Run the watcher with `.venv/bin/python` after that, including in cron, so a CSS watch can import BeautifulSoup.

## Twilio

1. Create a [Twilio](https://www.twilio.com/) account and buy a phone number (or use a trial number).
2. Copy the Account SID and Auth Token from the Twilio console.
3. Trial accounts can text only numbers you verify in the console, and Twilio prefixes the message with a trial notice. A paid send is a few cents per recipient. This script sends once per number.

## Configure Twilio

On the server, from this directory:

```bash
cp config.example.env .env
chmod 600 .env
```

Edit `.env`:

```
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
OUTAGE_TO_NUMBER=+1XXXXXXXXXX
```

Numbers are E.164 (`+` and country code). `OUTAGE_TO_NUMBER` receives one text when the page fails 10 checks in a row. The numbers that receive the watch alert belong to each config file, in `to_numbers`.

Check that every required setting is filled in:

```bash
./check_config.sh
```

The watcher, `./check_config.sh`, and the test scripts warn when any of these are missing or not a valid phone number: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `OUTAGE_TO_NUMBER`. The watcher keeps running and writes that warning to the log. `--dry-run` does not need Twilio settings.

## Configure the watch

Copy the example and edit it, or write your own file:

```bash
cp config.example.json config.json
```

Run it with:

```bash
python3 watchpage.py --config config.json
```

`to_numbers` is the list of E.164 numbers that receive the text for this watch. `name` is used in the default cron comment (`watchpage:<name>`) and the default state file (`state/<name>.json`). `message` may include `{url}` and `{name}`. It defaults to `Change detected: {url}`. `must_contain` is optional. When set, a page that lacks that text is skipped and does not alert. `cron_marker` defaults to `watchpage:<name>`. `state_file` defaults to `state/<name>.json`, relative to this directory.

`watch.kind` is `text` or `css`. `watch.alert_when` is `present` or `absent`. Text matching ignores case.

### Text, alert when the phrase is gone

This is [config.example.json](config.example.json). It texts when a normal Christmas Town page no longer contains `will be available`. `must_contain` skips a page that is not Christmas Town, so a wrong page does not look like a hit. `cron_marker` and `state_file` keep the job and state file that are already on the server.

```json
{
  "name": "christmas-town",
  "url": "https://www.conservationhalton.ca/christmas-town/",
  "to_numbers": ["+14165550101", "+14165550102"],
  "message": "Christmas Town tickets may be on sale: {url}",
  "must_contain": "christmas town",
  "cron_marker": "christmas-town-watch",
  "state_file": "state.json",
  "watch": {
    "kind": "text",
    "value": "will be available",
    "alert_when": "absent"
  }
}
```

### Text, alert when the phrase shows up

Texts when the page contains `add to cart`.

```json
{
  "name": "shop-cart",
  "url": "https://example.com/product",
  "to_numbers": ["+14165550101"],
  "message": "Add to cart is on the page: {url}",
  "watch": {
    "kind": "text",
    "value": "add to cart",
    "alert_when": "present"
  }
}
```

### CSS, alert when the element shows up

Texts when `a.buy-button` matches. Install BeautifulSoup first.

```json
{
  "name": "buy-button",
  "url": "https://example.com/tickets",
  "to_numbers": ["+14165550101"],
  "message": "The buy button is on the page: {url}",
  "watch": {
    "kind": "css",
    "value": "a.buy-button",
    "alert_when": "present"
  }
}
```

### CSS, alert when the element is gone

Texts when `.sold-out` no longer matches.

```json
{
  "name": "sold-out",
  "url": "https://example.com/tickets",
  "to_numbers": ["+14165550101"],
  "message": "The sold-out notice is gone: {url}",
  "watch": {
    "kind": "css",
    "value": ".sold-out",
    "alert_when": "absent"
  }
}
```

## Install the cron job

On the server, run `crontab -e` and add a line for each config. The comment at the end must match that config's `cron_marker`. After the texts go out, the script comments out that line. The line stays in the file. The marker is read from the comment, so a directory path that happens to contain the same words does not disable a different watch.

For the Christmas Town example, the marker is `christmas-town-watch`:

```cron
* * * * * flock -n /home/cstigas/christmas-town-watch/watch.lock /home/cstigas/christmas-town-watch/.venv/bin/python /home/cstigas/christmas-town-watch/watchpage.py --config /home/cstigas/christmas-town-watch/config.json >> /home/cstigas/christmas-town-watch/watch.log 2>&1 # christmas-town-watch
```

Another watch uses its own lock, log, config, and marker. With the default marker for `name` `shop-cart`:

```cron
* * * * * flock -n /home/cstigas/christmas-town-watch/shop-cart.lock /home/cstigas/christmas-town-watch/.venv/bin/python /home/cstigas/christmas-town-watch/watchpage.py --config /home/cstigas/christmas-town-watch/shop-cart.json >> /home/cstigas/christmas-town-watch/shop-cart.log 2>&1 # watchpage:shop-cart
```

`flock` skips a run if the previous one is still going. Confirm the job with `crontab -l`.

## See whether the watch is triggered

This fetches the page and prints one line. It does not send a text, write state, or edit crontab. It still checks the page after every recipient has already been notified.

```bash
python3 watchpage.py --config config.json --dry-run
```

- `watch triggered` means the condition is met
- `watch not triggered` means the page was checked and the condition is not met
- `watch not checked: ...` means the fetch failed, the body was too short, or `must_contain` was missing

Exit 0 for triggered and not triggered. Exit 1 for not checked.

## Test the text

This sends the real message and leaves the watcher running. It does not write state and does not remove cron.

```bash
python3 watchpage.py --config config.json --test-sms
```

`./test_outage.sh` times out 10 times against a local server that accepts the connection and never answers, then sends one outage text to `OUTAGE_TO_NUMBER`. It does not read a watch config, write state, or edit crontab.

## Watch the log

```bash
tail -f watch.log
```

A page that does not meet the condition logs `still waiting` once a minute. A failed fetch is logged and retried on the next run. After 10 failures in a row, one text goes to `OUTAGE_TO_NUMBER`. Later failures stay quiet until a check succeeds, which clears the count so a later outage can text again. When the watch condition is met, the log shows `watch triggered`, then `sent to +1...` for each number, then `commented out <marker> in crontab`.

## How a watch is decided

All of these must be true before a text is sent:

- The request returns HTTP 200 and a full page (at least 500 characters)
- If `must_contain` is set, that text is in the body
- The watch condition is met

Text is a case-insensitive substring. CSS uses BeautifulSoup's `select` on the HTML. `alert_when` `present` texts when the phrase or selector matches. `alert_when` `absent` texts when it does not.

Each successful Twilio send is stored in the state file immediately. A retry texts only numbers that have not been recorded. When every number is recorded, the script comments out the cron line and, on any later run, exits before requesting the website.

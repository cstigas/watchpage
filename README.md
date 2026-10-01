# watchpage

watchpage texts you when a web page changes in the way you name, then stays quiet.

A ticket page can say "will be available" for weeks, then switch to on sale. A product page can gain an Add to cart button, or lose a sold-out notice. Refreshing by hand is easy to forget. You give watchpage the URL and the phrase or element to watch. Each run fetches the page. While the condition is unmet, it sends nothing. When the condition is met, each phone number for that watch gets one SMS.

You decide how often it runs. Cron on macOS or Linux is enough: every minute, every hour, or once a day are the same program with a different schedule.

## One text for the change

A watcher that kept texting after the change would be annoying. watchpage de-duplicates the alert so each number gets it once.

Every successful SMS is recorded against that number as soon as Twilio accepts it. The next run texts only numbers still missing from the record, which retries a failed send and skips one that already went out. When every number has been texted, watchpage comments out its own cron line. The job stops, so later checks never happen and the same text is not sent again. The line stays in your crontab with a `#` in front of it. Remove the `#` when you want to watch for the next change.

If you run the command after that, it sees the completed record and exits. It does not fetch the page, and it does not send another SMS.

## When the page cannot be read

A failed fetch is a different event from the change you are watching for. If the site is unreachable, the request times out, or the response is any status other than HTTP 200, watchpage logs the failure and tries again on the next run. The schedule stays on. A body shorter than 500 characters is treated the same way, so a stub or an error page cannot look like the phrase you are waiting on has disappeared.

After 10 of those failures in a row, one SMS goes to `OUTAGE_TO_NUMBER`. Failures after that send nothing more. The next check that returns a full HTTP 200 page clears the count, so a later outage can text again. An outage text leaves the watch running. The alert for the change has not been sent.

A page that loads but lacks `must_contain` is skipped. That run sends nothing, and it does not add to the failure count.

## How alerts are sent

When a watch is triggered, or when a page has failed 10 checks in a row, watchpage sends an SMS through [Twilio](https://www.twilio.com/). Twilio delivers the text from a phone number you control. Watch alerts go to the numbers in that watch's config. The outage alert goes to `OUTAGE_TO_NUMBER` in `.env`.

## Dependencies

watchpage needs Python 3.10 or newer. It runs on macOS and Linux. Text watches use the Python standard library. A CSS watch needs BeautifulSoup. Importing cookies from Chrome, Chromium, Brave, or Edge needs the cryptography package, which `./setup.sh` installs. Firefox import uses the standard library. A watch that must run the page's JavaScript needs a headless browser, installed separately because that check is the exception.

`./setup.sh` creates a `.venv` in this directory and installs whatever is missing from `requirements.txt`. It does not need root. If `python3 -m venv` is missing, install the venv module for your Python. On Debian and Ubuntu that package is `python3-venv`.

```bash
./setup.sh
```

After the small install, the script asks whether to install headless Chromium. The default answer is no. Answer yes only for a watch that sets `render_javascript` to `true`. That check is slower, uses much more memory, and is no longer a simple download. Pressing Enter skips it. It then asks whether to install a cron job. Pressing Enter skips that too.

Run the watcher with `.venv/bin/python` after that, including from cron, so a CSS watch can import BeautifulSoup.

On a console-only Ubuntu server the browser does not need X Windows. Chromium still needs its system libraries. If it fails to start, install those once with `sudo .venv/bin/python -m playwright install-deps chromium`, then run `./setup.sh` again and answer yes. `./run_tests.sh` runs every test in `tests/`, including a check that headless Chromium can start and read text added by JavaScript. It does not send SMS. `./run_tests.sh --list` prints the test names. Pass `-t` or `--test` with a name, such as `./run_tests.sh --test test_browser`, to run one test.

## Configure Twilio

Create a Twilio account and buy a phone number, or use a trial number. Copy the Account SID and Auth Token from the Twilio console. Trial accounts can text only numbers you verify in the console, and Twilio prefixes the message with a trial notice.

From this directory on the machine that will run the checks:

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

Numbers are E.164 (`+` and country code). `OUTAGE_TO_NUMBER` receives the single text after 10 failed checks in a row. The numbers that receive the watch alert belong in each config file, under `to_numbers`.

Check that the required settings are filled in:

```bash
./check_config.sh
```

The watcher, `./check_config.sh`, and `./run_tests.sh` warn when any of these are missing or not a valid phone number: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `OUTAGE_TO_NUMBER`. The watcher keeps running and writes that warning to the log. `--dry-run` does not need Twilio settings.

## Configure the watch

One file is one watch. A second page gets its own file, state, and cron line, so delivering one alert leaves the others running.

```bash
cp config.example.json config.json
```

```bash
python3 watchpage.py --config config.json
```

- `url` is the page to fetch.
- `to_numbers` is the list of E.164 numbers that receive this watch's text.
- `watch.kind` is `text` or `css`. `watch.value` is the phrase, or a CSS selector. `watch.alert_when` is `present` or `absent`. Text matching ignores case.
- `message` is the SMS body. `{url}` and `{name}` are replaced. The default is `Change detected: {url}`.
- `must_contain` is optional. A page without that text is skipped.
- `name` is a short id. It sets the default cron comment, `watchpage:<name>`, and the default state file, `state/<name>.json`.
- `cron_marker` is the comment watchpage looks for when it disables the job after the alert. The default is `watchpage:<name>`.
- `state_file` stores who has been texted and the failure count. The default is `state/<name>.json`, relative to this directory.
- `render_javascript` is optional and defaults to `false`. `true` renders the page in a headless browser before the check. Leave it off unless the text you care about is missing from the first HTML response. A rendered check is slower, uses much more memory, and is no longer a simple download.
- `cookies_file` is optional. It is a Netscape cookie file sent on each fetch. A relative path is under this directory. Leave it out and the fetch sends no cookies.
- `user_agent` is optional. When omitted, the request uses `watchpage/1.0`. Set it to the browser's User-Agent when a clearance cookie only works with that agent.

### Text, alert when the phrase is gone

Texts when a tickets page no longer contains `will be available`. `must_contain` skips a page that does not mention the event, so the wrong page cannot look like a hit.

```json
{
  "name": "summer-tickets",
  "url": "https://example.com/tickets",
  "to_numbers": ["+14165550101", "+14165550102"],
  "message": "Tickets may be on sale: {url}",
  "must_contain": "summer concert",
  "cron_marker": "watchpage:summer-tickets",
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

### Page built by JavaScript

The four watches above download the HTML and stop there. Playwright is not imported. Set `render_javascript` to `true` when a script adds the text after the page loads. Run `./setup.sh` and answer yes when it asks about headless Chromium. This check is slower, uses much more memory, and is no longer a simple download.

```json
{
  "name": "js-sold-out",
  "url": "https://example.com/tickets",
  "to_numbers": ["+14165550101"],
  "message": "The sold-out notice is gone: {url}",
  "render_javascript": true,
  "watch": {
    "kind": "css",
    "value": ".sold-out",
    "alert_when": "absent"
  }
}
```

## Import cookies from a browser

Use this when you are already logged in, or you have already passed a captcha, and the watch should see that same page. The import reads one browser profile and one domain. It does not scan every browser, and it does not take a parent domain or any other subdomain. Cron does not talk to the browser. It sends the cookie file written here.

On a terminal, the command asks which browser, which profile when there are several, and which domain. The suggested domain is the host from `url`. A cookie set on a parent domain, such as `example.com` for a page on `www.example.com`, is included only if you enter that parent domain.

```bash
.venv/bin/python watchpage.py --config config.json --import-cookies
.venv/bin/python watchpage.py --config config.json --import-cookies --browser chrome --domain www.example.com
```

`--browser` is `chrome`, `chromium`, `brave`, `edge`, `firefox`, or `safari`. Safari is macOS only. From cron, or any run without a terminal, pass both `--browser` and `--domain`. If that browser has more than one profile, pass `--profile` as well.

The command writes `cookies/<name>.txt` when the config has no `cookies_file`, and prints the line to add. When `cookies_file` is already set, it rewrites that file. The file mode is `0600`. The output shows the browser, the domain, the count, and the path. It does not show cookie names or values.

Chrome, Chromium, Brave, and Edge keep cookie values encrypted. On macOS, Keychain may prompt once for the Safe Storage password. On Linux, install `secretstorage` if the key is in the keyring (`.venv/bin/python -m pip install secretstorage`). The import decrypts only the rows for the domain you named. If the cookies are not Chrome's v10 format, export a Netscape `cookies.txt` yourself and set `cookies_file` to that path. Reading Safari's cookie file can require Full Disk Access for the terminal.

A later check sends those cookies and, when the page comes back in full, saves `Set-Cookie` back into the file so a session can refresh. `--dry-run` sends the cookies and does not write the file. A clearance cookie usually works only from this machine, with the same User-Agent, until it expires. This does not solve captchas.

```json
{
  "name": "summer-tickets",
  "url": "https://www.example.com/tickets",
  "to_numbers": ["+14165550101"],
  "cookies_file": "cookies/summer-tickets.txt",
  "user_agent": "Mozilla/5.0",
  "watch": {
    "kind": "text",
    "value": "will be available",
    "alert_when": "absent"
  }
}
```

## Schedule a check

`./setup.sh` can install the cron line. When several watch configs are in this directory, it asks which one. The schedule defaults to every minute. The line uses absolute paths, names the lock and log files from the config's `name`, and ends with that config's `cron_marker`. Running `./setup.sh` again does not add a second copy. If that line is already commented out, the script offers to uncomment it.

To add the line yourself, run `crontab -e` and add one line per config. The five fields at the start of the line are the schedule. `* * * * *` runs every minute; change them to whatever interval you want. The comment at the end must match that config's `cron_marker`. After every number has received the alert, watchpage finds the line by that comment and comments it out, which is what stops the repeat texts. The marker is read only from the comment, so a directory path that contains the same words does not disable a different watch.

For the tickets example, the marker is `watchpage:summer-tickets`. Use the directory where you installed watchpage in place of `/path/to/watchpage`:

```cron
* * * * * flock -n /path/to/watchpage/watch.lock /path/to/watchpage/.venv/bin/python /path/to/watchpage/watchpage.py --config /path/to/watchpage/config.json >> /path/to/watchpage/watch.log 2>&1 # watchpage:summer-tickets
```

Another watch uses its own lock, log, config, and marker. With the default marker for `name` `shop-cart`:

```cron
* * * * * flock -n /path/to/watchpage/shop-cart.lock /path/to/watchpage/.venv/bin/python /path/to/watchpage/watchpage.py --config /path/to/watchpage/shop-cart.json >> /path/to/watchpage/shop-cart.log 2>&1 # watchpage:shop-cart
```

`flock` skips a run when the previous one is still going. It is standard on Linux. On macOS, install it or leave it off the command. `./setup.sh` includes `flock` when that command is installed. When it is not, the script prints a warning before adding the line. Confirm the job with `crontab -l`.

## See whether the watch is triggered

This fetches the page and prints one line. It sends no text, writes no state, does not update the cookie file, and does not edit crontab. It fetches even when a normal run would exit because every number was already texted.

```bash
python3 watchpage.py --config config.json --dry-run
```

- `watch triggered` means the condition is met
- `watch not triggered` means the page was checked and the condition is unmet
- `watch not checked: ...` means the fetch failed, the body was under 500 characters, or `must_contain` was missing

Exit 0 for triggered and not triggered. Exit 1 for not checked.

## Test the text

This sends the real message. It does not record the send and does not comment out cron, so the scheduled watch can still alert later.

```bash
python3 watchpage.py --config config.json --test-sms
```

`./run_tests.sh --test test_outage` runs the outage checks in `tests/` and does not send a text. `./run_tests.sh --test test_outage_with_send` opens a local server that accepts the connection and sends nothing back. The watcher times out against it 10 times, then sends one outage text to `OUTAGE_TO_NUMBER`. It does not read a watch config, write state, or edit crontab. `./run_tests.sh --test test_sms_send` sends one test text to each number in the watch config. Neither send runs unless you name it.

## Watch the log

```bash
tail -f watch.log
```

A page that does not meet the condition logs `still waiting`. A failed fetch is logged and counted. On the 10th failure in a row the log shows `outage alert sent to +1...`. When the condition is met, the log shows `watch triggered`, then `sent to +1...` for each number, then `commented out <marker> in crontab`.

## What has to be true before the text goes out

- The check returns HTTP 200 and a body of at least 500 characters. With `render_javascript` that body is the page after scripts run
- If `must_contain` is set, that text is in the body
- The watch condition is met

Text is a case-insensitive substring. CSS uses BeautifulSoup's `select` on the HTML. `alert_when` `present` texts when the phrase or selector matches. `alert_when` `absent` texts when it does not.

#!/usr/bin/env python3
"""Watch an HTTP page and text when the configured condition is met.

Once every configured number has been texted, later runs exit before
fetching the page and comment out this job in crontab.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "state.json"

TEST_MODE = "DONT_UPDATE_STATE"
MIN_BODY_LENGTH = 500
FETCH_TIMEOUT_SECONDS = 30
OUTAGE_FAILURE_THRESHOLD = 10
USER_AGENT = "watchpage/1.0"
DEFAULT_MESSAGE = "Change detected: {url}"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{stamp} {message}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


REQUIRED_SETTINGS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "TO_NUMBERS",
    "OUTAGE_TO_NUMBER",
)


def valid_e164(number: str) -> bool:
    cleaned = "".join(number.split())
    digits = cleaned[1:] if cleaned.startswith("+") else ""
    return cleaned.startswith("+") and digits.isdigit() and len(digits) >= 8


def require_e164(label: str, number: str) -> str:
    cleaned = "".join(number.split())
    if not valid_e164(cleaned):
        raise SystemExit(
            f"{label} must be an E.164 number such as +14165550100, got {number!r}"
        )
    return cleaned


def config_warnings(env_path: Path | None = None) -> list[str]:
    """Return warnings for required settings that are missing or not usable."""
    path = ROOT / ".env" if env_path is None else env_path
    if not path.is_file():
        return [f"{path.name} is missing"]

    values = load_env_file(path)
    for key in REQUIRED_SETTINGS:
        if os.environ.get(key):
            values[key] = os.environ[key]

    warnings = []
    for key in REQUIRED_SETTINGS:
        raw = str(values.get(key, "")).strip()
        if not raw:
            warnings.append(f"{key} is not set")
            continue
        if key == "TO_NUMBERS":
            numbers = [part.strip() for part in raw.split(",") if part.strip()]
            if not numbers:
                warnings.append("TO_NUMBERS is not set")
            for number in numbers:
                if not valid_e164(number):
                    warnings.append(f"TO_NUMBERS entry {number} is not an E.164 number")
        elif key in ("TWILIO_FROM_NUMBER", "OUTAGE_TO_NUMBER") and not valid_e164(raw):
            warnings.append(f"{key} is not an E.164 number")
    return warnings


def warn_config(env_path: Path | None = None) -> int:
    warnings = config_warnings(env_path)
    for warning in warnings:
        log(f"warning: {warning}")
    return len(warnings)


def load_config() -> dict[str, object]:
    file_values = load_env_file(ENV_PATH)
    merged = dict(file_values)
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "TO_NUMBERS",
        "OUTAGE_TO_NUMBER",
    ):
        if os.environ.get(key):
            merged[key] = os.environ[key]

    missing = [
        key
        for key in (
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "TO_NUMBERS",
        )
        if not str(merged.get(key, "")).strip()
    ]
    if missing:
        raise SystemExit(
            "Missing "
            + ", ".join(missing)
            + f". Copy {ENV_PATH.name} from config.example.env and fill it in."
        )

    recipients = []
    seen: set[str] = set()
    for raw in str(merged["TO_NUMBERS"]).split(","):
        if not raw.strip():
            continue
        number = require_e164("TO_NUMBERS entry", raw)
        if number not in seen:
            seen.add(number)
            recipients.append(number)
    if not recipients:
        raise SystemExit("TO_NUMBERS is empty.")

    outage_raw = str(merged.get("OUTAGE_TO_NUMBER") or "").strip()
    outage_number = require_e164("OUTAGE_TO_NUMBER", outage_raw) if outage_raw else ""

    return {
        "account_sid": str(merged["TWILIO_ACCOUNT_SID"]).strip(),
        "auth_token": str(merged["TWILIO_AUTH_TOKEN"]).strip(),
        "from_number": require_e164(
            "TWILIO_FROM_NUMBER", str(merged["TWILIO_FROM_NUMBER"])
        ),
        "recipients": recipients,
        "outage_number": outage_number,
    }


def config_error(path: Path, message: str) -> None:
    print(f"Parsing error on {path.name}: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def require_text_key(path: Path, obj: dict, key: str) -> str:
    if key not in obj:
        config_error(path, f"Missing {key} key")
    value = obj[key]
    if not isinstance(value, str) or not value.strip():
        config_error(path, f"{key} is empty")
    return value.strip()


def load_watch_config(path: Path) -> dict[str, object]:
    """Load the page URL and watch condition from a JSON file."""
    if not path.is_file():
        config_error(path, "file not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        config_error(path, f"invalid JSON ({exc})")
    if not isinstance(data, dict):
        config_error(path, "file is not a JSON object")

    name = require_text_key(path, data, "name")
    if not NAME_RE.fullmatch(name):
        config_error(
            path, "name must be letters, digits, dots, hyphens, or underscores"
        )

    url = require_text_key(path, data, "url")
    if not url.startswith(("http://", "https://")):
        config_error(path, "url must start with http:// or https://")

    if "watch" not in data:
        config_error(path, "Missing watch key")
    watch = data["watch"]
    if not isinstance(watch, dict):
        config_error(path, "watch must be an object")
    kind = require_text_key(path, watch, "kind")
    if kind not in ("text", "css"):
        config_error(path, 'kind must be "text" or "css"')
    alert_when = require_text_key(path, watch, "alert_when")
    if alert_when not in ("present", "absent"):
        config_error(path, 'alert_when must be "present" or "absent"')
    value = require_text_key(path, watch, "value")
    if kind == "css":
        try:
            validate_css(value)
        except SystemExit as exc:
            detail = exc.code if isinstance(exc.code, str) else "invalid CSS selector"
            config_error(path, detail)

    if "message" in data:
        message = require_text_key(path, data, "message")
    else:
        message = DEFAULT_MESSAGE

    must_contain = ""
    if "must_contain" in data:
        must_contain = require_text_key(path, data, "must_contain")

    if "cron_marker" in data:
        cron_marker = require_text_key(path, data, "cron_marker")
    else:
        cron_marker = f"watchpage:{name}"

    if "state_file" in data:
        state_raw = require_text_key(path, data, "state_file")
        state_file = Path(state_raw)
        if not state_file.is_absolute():
            state_file = ROOT / state_file
    else:
        state_file = ROOT / "state" / f"{name}.json"

    return {
        "name": name,
        "url": url,
        "page_url": url,
        "message": message,
        "must_contain": must_contain,
        "cron_marker": cron_marker,
        "state_file": state_file,
        "watch": {"kind": kind, "value": value, "alert_when": alert_when},
    }


def import_beautifulsoup():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit(
            "CSS watches need beautifulsoup4. Install it with: ./setup.sh"
        ) from exc
    return BeautifulSoup


def validate_css(selector: str) -> None:
    beautiful_soup = import_beautifulsoup()
    try:
        beautiful_soup("", "html.parser").select(selector)
    except Exception as exc:
        raise SystemExit(f"Invalid CSS selector {selector!r}: {exc}") from exc


def empty_state() -> dict[str, object]:
    return {
        "sent_to": [],
        "completed_at": None,
        "consecutive_failures": 0,
        "outage_alerted": False,
    }


def load_state() -> dict[str, object]:
    if not STATE_PATH.is_file():
        return empty_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not read {STATE_PATH.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{STATE_PATH.name} is not a JSON object.")
    sent = data.get("sent_to", [])
    if not isinstance(sent, list) or not all(isinstance(item, str) for item in sent):
        raise SystemExit(f"{STATE_PATH.name} has an invalid sent_to list.")
    failures = data.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise SystemExit(f"{STATE_PATH.name} has an invalid consecutive_failures value.")
    alerted = data.get("outage_alerted", False)
    if not isinstance(alerted, bool):
        raise SystemExit(f"{STATE_PATH.name} has an invalid outage_alerted value.")
    data["sent_to"] = sent
    data.setdefault("completed_at", None)
    data["consecutive_failures"] = failures
    data["outage_alerted"] = alerted
    return data


def in_test_mode() -> bool:
    return os.environ.get(TEST_MODE) == "1"


def save_state(state: dict[str, object]) -> None:
    if in_test_mode():
        raise SystemExit("refusing to write state.json during a test")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)


def outage_text(name: str, count: int, url: str) -> str:
    return f"{name} page unreachable for {count} checks: {url}"


def note_fetch_failure(config: dict[str, object], state: dict[str, object]) -> int:
    count = int(state["consecutive_failures"]) + 1
    state["consecutive_failures"] = count
    save_state(state)
    log(f"consecutive fetch failures: {count}")
    if count < OUTAGE_FAILURE_THRESHOLD or state["outage_alerted"]:
        return 0

    number = str(config["outage_number"])
    if not number:
        log("OUTAGE_TO_NUMBER is not set; outage not texted")
        return 0

    body = outage_text(str(config["name"]), count, str(config["page_url"]))
    if send_sms(config, number, body):
        state["outage_alerted"] = True
        save_state(state)
        log(f"outage alert sent to {number}")
        return 0
    log("outage alert was not sent; will retry")
    return 0


def clear_fetch_failures(state: dict[str, object]) -> None:
    if not state["consecutive_failures"] and not state["outage_alerted"]:
        return
    state["consecutive_failures"] = 0
    state["outage_alerted"] = False
    save_state(state)


def run_outage_test(config: dict[str, object]) -> int:
    """Time out against a local server that accepts and never answers, then text once.

    The watched site cannot be asked to time out on cue. A socket on
    127.0.0.1 that accepts the connection and sends nothing fails the same way
    every time. The failure count stays in memory, so state is not written.
    """
    global FETCH_TIMEOUT_SECONDS
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    port = server.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_and_hold() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            held.append(connection)

    thread = threading.Thread(target=accept_and_hold, daemon=True)
    thread.start()
    original_timeout = FETCH_TIMEOUT_SECONDS
    FETCH_TIMEOUT_SECONDS = 1
    try:
        log(f"outage test timing out against {url}")
        state = empty_state()
        for _ in range(OUTAGE_FAILURE_THRESHOLD):
            body, _problem = fetch_page(url)
            if body is not None:
                log("outage test received a page from the hung server")
                return 1
            count = int(state["consecutive_failures"]) + 1
            state["consecutive_failures"] = count
            log(f"consecutive fetch failures: {count}")
        number = str(config["outage_number"])
        if not number:
            log("OUTAGE_TO_NUMBER is not set; outage not texted")
            return 1
        body_text = f"Outage test: unreachable for {OUTAGE_FAILURE_THRESHOLD} checks: {url}"
        if not send_sms(config, number, body_text):
            return 1
        log(f"outage alert sent to {number}")
        return 0
    finally:
        FETCH_TIMEOUT_SECONDS = original_timeout
        stop.set()
        for connection in held:
            connection.close()
        server.close()


def pending_recipients(recipients: list[str], state: dict[str, object]) -> list[str]:
    sent = set(state["sent_to"])
    return [number for number in recipients if number not in sent]


def fetch_page(url: str) -> tuple[str | None, str | None]:
    """Return (body, None) or (None, reason)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            status = response.status
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        log(f"fetch failed: HTTP {exc.code}")
        return None, "fetch failed"
    except Exception as exc:
        log(f"fetch failed: {exc}")
        return None, "fetch failed"

    if status != 200:
        log(f"fetch failed: HTTP {status}")
        return None, "fetch failed"
    if len(body) < MIN_BODY_LENGTH:
        log(f"page too short ({len(body)} bytes); skipping")
        return None, "page too short"
    return body, None


def page_matches(body: str, watch: dict[str, str]) -> bool:
    kind = watch["kind"]
    value = watch["value"]
    if kind == "text":
        return value.casefold() in body.casefold()
    beautiful_soup = import_beautifulsoup()
    try:
        found = beautiful_soup(body, "html.parser").select(value)
    except Exception as exc:
        raise SystemExit(f"Invalid CSS selector {value!r}: {exc}") from exc
    return bool(found)


def classify_page(body: str, config: dict[str, object]) -> str:
    """Return triggered, waiting, or unexpected."""
    must = str(config.get("must_contain") or "")
    if must and must.casefold() not in body.casefold():
        return "unexpected"
    watch = config["watch"]
    if not isinstance(watch, dict):
        raise SystemExit("Config watch must be an object.")
    matched = page_matches(body, watch)
    if watch["alert_when"] == "present":
        return "triggered" if matched else "waiting"
    return "waiting" if matched else "triggered"


def assess(
    body: str | None, problem: str | None, config: dict[str, object]
) -> tuple[str, str]:
    """Return (triggered|not_triggered|not_checked, detail)."""
    if body is None:
        return "not_checked", problem or "fetch failed"
    status = classify_page(body, config)
    if status == "unexpected":
        return "not_checked", "page did not contain the expected text"
    if status == "triggered":
        return "triggered", ""
    return "not_triggered", ""


def message_body(config: dict[str, object]) -> str:
    template = str(config["message"])
    return template.replace("{url}", str(config["page_url"])).replace(
        "{name}", str(config["name"])
    )


def send_sms(config: dict[str, object], to_number: str, body: str) -> bool:
    account_sid = str(config["account_sid"])
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{urllib.parse.quote(account_sid)}/Messages.json"
    )
    payload = urllib.parse.urlencode(
        {
            "From": config["from_number"],
            "To": to_number,
            "Body": body,
        }
    ).encode()
    token = str(config["auth_token"])
    auth = base64.b64encode(f"{account_sid}:{token}".encode()).decode()
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Authorization", f"Basic {auth}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            response.read()
            if 200 <= response.status < 300:
                return True
            log(f"Twilio rejected {to_number}: HTTP {response.status}")
            return False
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        log(f"Twilio rejected {to_number}: HTTP {exc.code} {detail}")
        return False
    except Exception as exc:
        log(f"Twilio request failed for {to_number}: {exc}")
        return False


def comment_contains_marker(line: str, marker: str) -> bool:
    """True when marker is its own token in the crontab comment.

    The directory path can contain the same words. Only the comment decides
    which job to comment out, so one watcher does not disable another.
    """
    if line.lstrip().startswith("#"):
        return False
    hash_at = line.find("#")
    if hash_at < 0:
        return False
    comment = line[hash_at + 1 :]
    name_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    start = 0
    while True:
        index = comment.find(marker, start)
        if index < 0:
            return False
        end = index + len(marker)
        before_ok = index == 0 or comment[index - 1] not in name_chars
        after_ok = end == len(comment) or comment[end] not in name_chars
        if before_ok and after_ok:
            return True
        start = index + 1


def comment_out_cron(marker: str) -> None:
    """Comment out the watcher line. The crontab entry is left in place."""
    if in_test_mode():
        raise SystemExit("refusing to edit crontab during a test")
    try:
        listed = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        log("crontab is not installed; comment out the scheduled job by hand")
        return

    if listed.returncode != 0:
        log("no crontab installed")
        return

    updated = []
    changed = False
    for line in listed.stdout.splitlines():
        if comment_contains_marker(line, marker):
            updated.append("# " + line)
            changed = True
        else:
            updated.append(line)

    if not changed:
        log(f"no active crontab line contains {marker}")
        return

    result = subprocess.run(
        ["crontab", "-"],
        input="\n".join(updated) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log(f"could not update crontab: {result.stderr.strip()}")
        return
    log(f"commented out {marker} in crontab")


def finish_if_complete(config: dict[str, object], state: dict[str, object]) -> bool:
    recipients = list(config["recipients"])
    if pending_recipients(recipients, state):
        return False
    if not state.get("completed_at"):
        state["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_state(state)
    log("all recipients already notified")
    comment_out_cron(str(config["cron_marker"]))
    return True


def send_pending(
    config: dict[str, object],
    state: dict[str, object],
    record: bool,
) -> int:
    recipients = list(config["recipients"])
    targets = recipients if not record else pending_recipients(recipients, state)
    body = message_body(config)
    failed = False
    for number in targets:
        if send_sms(config, number, body):
            log(f"sent to {number}")
            if record:
                sent = list(state["sent_to"])
                sent.append(number)
                state["sent_to"] = sent
                save_state(state)
        else:
            failed = True
    if not record:
        return 1 if failed else 0
    if failed or pending_recipients(recipients, state):
        log("some recipients were not notified; will retry")
        return 1
    state["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_state(state)
    log("all recipients notified")
    comment_out_cron(str(config["cron_marker"]))
    return 0


def run_dry(config: dict[str, object]) -> int:
    body, problem = fetch_page(str(config["page_url"]))
    outcome, detail = assess(body, problem, config)
    if outcome == "not_checked":
        log(f"watch not checked: {detail}")
        return 1
    if outcome == "triggered":
        log("watch triggered")
        return 0
    log("watch not triggered")
    return 0


def main(argv: list[str] | None = None) -> int:
    global STATE_PATH
    parser = argparse.ArgumentParser(
        description="Watch an HTTP page and text when the configured condition is met."
    )
    parser.add_argument(
        "--config",
        help="JSON file with the page URL and what to watch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch the page and report whether the watch is triggered. Does not send texts or change state.",
    )
    parser.add_argument(
        "--test-sms",
        action="store_true",
        help="Send a test text without saving state or commenting out cron.",
    )
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="If the watch is triggered, send the alert without saving state or commenting out cron.",
    )
    parser.add_argument(
        "--test-outage",
        action="store_true",
        help="Time out against a local hung server and send one outage text without saving state.",
    )
    args = parser.parse_args(argv)
    if args.dry_run and (args.test_sms or args.no_record or args.test_outage):
        raise SystemExit("--dry-run cannot be combined with other test flags")
    if args.test_outage:
        warn_config()
        return run_outage_test(load_config())
    if not args.config:
        parser.error("the following arguments are required: --config")

    watch_config = load_watch_config(Path(args.config))
    STATE_PATH = Path(watch_config["state_file"])

    if args.dry_run:
        return run_dry(watch_config)

    warn_config()
    config = load_config()
    config.update(watch_config)
    state = load_state()
    recipients = list(config["recipients"])

    if args.test_outage:
        return run_outage_test(config)

    if args.test_sms:
        log("sending test SMS")
        return send_pending(config, state, record=False)

    if not args.no_record and finish_if_complete(config, state):
        return 0

    body, problem = fetch_page(str(config["page_url"]))
    if body is None:
        if args.no_record:
            return 0
        return note_fetch_failure(config, state)
    if not args.no_record:
        clear_fetch_failures(state)

    outcome, _detail = assess(body, problem, config)
    if outcome == "not_triggered":
        log("still waiting")
        return 0
    if outcome == "not_checked":
        log("page did not contain the expected text; skipping")
        return 0

    log("watch triggered")
    return send_pending(config, state, record=not args.no_record)


if __name__ == "__main__":
    sys.exit(main())

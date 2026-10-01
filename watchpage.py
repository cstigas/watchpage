#!/usr/bin/env python3
"""Watch an HTTP page and text when the configured condition is met.

Once every configured number has been texted, later runs exit before
fetching the page and comment out this job in crontab.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import cookie_import

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
RENDER_WAIT_SECONDS = 20
# Headless Chromium on a server: no GPU, and /dev/shm is often tiny.
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu"]
# Returns the page HTML once it shows the watched text or selector (and
# must_contain), or, with nothing to watch for, once the HTML is parsed.
READ_PAGE_JS = """({needle, selector, must}) => {
  const root = document.documentElement;
  if (!root) return false;
  const html = root.outerHTML;
  if (!needle && !selector) return document.readyState === 'loading' ? false : html;
  const lower = html.toLowerCase();
  if (must && !lower.includes(must)) return false;
  if (needle) return lower.includes(needle) ? html : false;
  try { return document.querySelector(selector) ? html : false; } catch (e) { return false; }
}"""


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
        if key in ("TWILIO_FROM_NUMBER", "OUTAGE_TO_NUMBER") and not valid_e164(raw):
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
        )
        if not str(merged.get(key, "")).strip()
    ]
    if missing:
        raise SystemExit(
            "Missing "
            + ", ".join(missing)
            + f". Copy {ENV_PATH.name} from config.example.env and fill it in."
        )

    outage_raw = str(merged.get("OUTAGE_TO_NUMBER") or "").strip()
    outage_number = require_e164("OUTAGE_TO_NUMBER", outage_raw) if outage_raw else ""

    return {
        "account_sid": str(merged["TWILIO_ACCOUNT_SID"]).strip(),
        "auth_token": str(merged["TWILIO_AUTH_TOKEN"]).strip(),
        "from_number": require_e164(
            "TWILIO_FROM_NUMBER", str(merged["TWILIO_FROM_NUMBER"])
        ),
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


def parse_to_numbers(path: Path, data: dict) -> list[str]:
    if "to_numbers" not in data:
        config_error(path, "Missing to_numbers key")
    raw = data["to_numbers"]
    if not isinstance(raw, list) or not raw:
        config_error(path, "to_numbers must be a non-empty list of phone numbers")
    recipients: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not valid_e164(item):
            config_error(path, f"to_numbers entry {item!r} is not an E.164 number")
        number = "".join(item.split())
        if number not in seen:
            seen.add(number)
            recipients.append(number)
    return recipients


def load_watch_config(
    path: Path, *, require_cookies_file: bool = True
) -> dict[str, object]:
    """Load the page URL and watch condition from a JSON file."""
    if not path.is_file():
        config_error(path, "file not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        config_error(path, f"invalid JSON ({exc})")
    if not isinstance(data, dict):
        config_error(path, "file is not a JSON object")

    recipients = parse_to_numbers(path, data)
    name = require_text_key(path, data, "name")
    if not NAME_RE.fullmatch(name):
        config_error(
            path, "name must be letters, digits, dots, hyphens, or underscores"
        )

    url = require_text_key(path, data, "url")
    if not url.startswith(("http://", "https://")):
        config_error(path, "url must start with http:// or https://")

    fetch_url = url
    if "check_url" in data:
        fetch_url = require_text_key(path, data, "check_url")
        if not fetch_url.startswith(("http://", "https://")):
            config_error(path, "check_url must start with http:// or https://")

    headers: dict[str, str] = {}
    if "headers" in data:
        raw_headers = data["headers"]
        if not isinstance(raw_headers, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in raw_headers.items()
        ):
            config_error(path, "headers must be an object of text values")
        headers = dict(raw_headers)

    if "watch" not in data:
        config_error(path, "Missing watch key")
    watch = data["watch"]
    if not isinstance(watch, dict):
        config_error(path, "watch must be an object")
    kind = require_text_key(path, watch, "kind")
    if kind not in ("text", "css", "json"):
        config_error(path, 'kind must be "text", "css", or "json"')
    alert_when = require_text_key(path, watch, "alert_when")
    if alert_when not in ("present", "absent"):
        config_error(path, 'alert_when must be "present" or "absent"')
    value = require_text_key(path, watch, "value")
    parsed_watch = {"kind": kind, "value": value, "alert_when": alert_when}
    selectors = [value] if kind == "css" else []
    if kind == "json":
        parsed_watch["path"] = require_text_key(path, watch, "path")
        if "script" in watch:
            parsed_watch["script"] = require_text_key(path, watch, "script")
            selectors.append(parsed_watch["script"])
    for selector in selectors:
        try:
            validate_css(selector)
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

    cookies_file = None
    if "cookies_file" in data:
        cookies_raw = require_text_key(path, data, "cookies_file")
        cookies_file = Path(cookies_raw)
        if not cookies_file.is_absolute():
            cookies_file = ROOT / cookies_file
        if require_cookies_file and not cookies_file.is_file():
            config_error(path, f"cookies file not found: {cookies_file}")

    user_agent = None
    if "user_agent" in data:
        user_agent = require_text_key(path, data, "user_agent")

    render_javascript = False
    if "render_javascript" in data:
        raw_render = data["render_javascript"]
        if not isinstance(raw_render, bool):
            config_error(path, "render_javascript must be true or false")
        render_javascript = raw_render

    return {
        "name": name,
        "recipients": recipients,
        "url": url,
        "page_url": url,
        "fetch_url": fetch_url,
        "headers": headers,
        "message": message,
        "must_contain": must_contain,
        "cron_marker": cron_marker,
        "state_file": state_file,
        "cookies_file": cookies_file,
        "user_agent": user_agent,
        "render_javascript": render_javascript,
        "watch": parsed_watch,
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


def import_playwright():
    """Import Playwright only for a watch that renders JavaScript."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "JavaScript rendering needs Playwright. Run ./setup.sh and answer yes when asked about headless Chromium."
        ) from exc
    return sync_playwright, PlaywrightTimeout


def load_cookie_jar(path: Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError) as exc:
        raise SystemExit(f"Could not read cookies file {path.name}: {exc}") from exc
    return jar


def save_cookie_jar(jar: http.cookiejar.MozillaCookieJar) -> None:
    if not jar.filename:
        raise SystemExit("cookies file has no path")
    path = Path(jar.filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=False)
    os.chmod(path, 0o600)


def playwright_cookie_payload(
    jar: http.cookiejar.CookieJar, url: str
) -> list[dict[str, object]]:
    request = urllib.request.Request(url)
    policy = jar._policy
    # CookieJar sets this before return_ok. Expiry checks read it.
    policy._now = int(datetime.now(timezone.utc).timestamp())
    payload: list[dict[str, object]] = []
    for cookie in jar:
        if not policy.return_ok(cookie, request):
            continue
        item: dict[str, object] = {
            "name": cookie.name,
            "value": cookie.value or "",
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": bool(cookie.secure),
            "httpOnly": cookie.has_nonstandard_attr("HTTPOnly"),
        }
        if cookie.expires is not None:
            item["expires"] = int(cookie.expires)
        payload.append(item)
    return payload


def store_playwright_cookies(jar: http.cookiejar.CookieJar, items: list[dict]) -> None:
    for item in items:
        name = item.get("name")
        value = item.get("value")
        host = item.get("domain")
        if not isinstance(name, str) or not isinstance(value, str) or not isinstance(host, str):
            continue
        expires_raw = item.get("expires", -1)
        expires = None
        if isinstance(expires_raw, (int, float)) and not isinstance(expires_raw, bool):
            if expires_raw >= 0:
                expires = int(expires_raw)
        jar.set_cookie(
            cookie_import.make_cookie(
                name=name,
                value=value,
                host=host,
                path=str(item.get("path") or "/"),
                secure=bool(item.get("secure")),
                http_only=bool(item.get("httpOnly")),
                expires=expires,
            )
        )


def _agent_debug(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # #region agent log
    payload = {
        "sessionId": "19edcd",
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(datetime.now().timestamp() * 1000),
    }
    line = json.dumps(payload, default=str)
    paths = []
    for path in (
        Path("/Users/cstigas/Projects/christmas-town-watch/.cursor/debug-19edcd.log"),
        ROOT / ".cursor" / "debug-19edcd.log",
    ):
        resolved = path.resolve()
        if resolved not in paths:
            paths.append(resolved)
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
    # #endregion


def render_page(
    url: str,
    *,
    user_agent: str | None = None,
    jar: http.cookiejar.MozillaCookieJar | None = None,
    save_cookies: bool = False,
    watch: dict[str, str] | None = None,
    must_contain: str = "",
    headers: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return the HTML after scripts run, using a headless browser.

    Load events are not a usable signal: trackers and long-lived requests can
    hold them back forever. The page is read as soon as it shows the watched
    text or selector, or after RENDER_WAIT_SECONDS if it never does. Without a
    watch, it is read once the HTML has been parsed.
    """
    sync_playwright, playwright_timeout = import_playwright()
    agent = user_agent or USER_AGENT
    log("rendering page in a headless browser")
    stage = "start"
    started = datetime.now().timestamp()
    probe = {"needle": "", "selector": "", "must": must_contain.lower()}
    if watch:
        if watch["kind"] == "text":
            probe["needle"] = watch["value"].lower()
        elif watch["kind"] == "css":
            probe["selector"] = watch["value"]
        elif watch.get("script"):
            probe["selector"] = watch["script"]
    # #region agent log
    _agent_debug(
        "A",
        "watchpage.py:render_page",
        "render start",
        {
            "has_cookies": jar is not None,
            "cookie_count": len(list(jar)) if jar is not None else 0,
            "watch_kind": watch["kind"] if watch else None,
        },
    )
    # #endregion
    try:
        with sync_playwright() as playwright:
            stage = "launch"
            browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            failure = None
            try:
                stage = "new_context"
                context = browser.new_context(
                    user_agent=agent, extra_http_headers=headers or {}
                )
                if jar is not None:
                    payload = playwright_cookie_payload(jar, url)
                    if payload:
                        stage = "add_cookies"
                        context.add_cookies(payload)
                stage = "new_page"
                page = context.new_page()

                # Scripts on the page may send the tab elsewhere, for example to
                # a sign-in page. Once the requested page has arrived, the tab
                # stays on it. "aborted" cancels the navigation without
                # replacing the page with an error page.
                stay: dict[str, object] = {"path": None, "blocked": [], "documents": []}

                def note_document(response) -> None:
                    request = response.request
                    if not request.is_navigation_request() or request.frame != page.main_frame:
                        return
                    path = urllib.parse.urlparse(response.url).path
                    # #region agent log
                    if len(stay["documents"]) < 8:
                        stay["documents"].append({"status": response.status, "path": path[:120]})
                    # #endregion
                    if stay["path"] is None and not 300 <= response.status < 400:
                        stay["path"] = path

                # #region agent log
                _marks: dict[str, int] = {}
                _routes = {"count": 0, "max_gap_ms": 0, "last": 0.0}

                def _mark(name: str) -> None:
                    _marks[name] = int((datetime.now().timestamp() - started) * 1000)
                # #endregion

                def stay_on_page(route) -> None:
                    # #region agent log
                    _now = datetime.now().timestamp()
                    if _routes["last"]:
                        _routes["max_gap_ms"] = max(
                            _routes["max_gap_ms"], int((_now - _routes["last"]) * 1000)
                        )
                    _routes["last"] = _now
                    _routes["count"] += 1
                    # #endregion
                    request = route.request
                    if (
                        stay["path"] is not None
                        and request.is_navigation_request()
                        and request.frame == page.main_frame
                        and urllib.parse.urlparse(request.url).path != stay["path"]
                    ):
                        # #region agent log
                        if len(stay["blocked"]) < 8:
                            stay["blocked"].append(urllib.parse.urlparse(request.url).path[:120])
                        # #endregion
                        route.abort("aborted")
                        return
                    route.fallback()

                page.on("response", note_document)
                page.route("**/*", stay_on_page)

                stage = "goto"
                # #region agent log
                _mark("page_ready")
                # #endregion
                response = page.goto(
                    url, wait_until="commit", timeout=FETCH_TIMEOUT_SECONDS * 1000
                )
                # #region agent log
                _mark("committed")
                # #endregion
                if response is None:
                    log("fetch failed: no response")
                    return None, "fetch failed"
                if response.status != 200:
                    log(f"fetch failed: HTTP {response.status}")
                    return None, "fetch failed"

                def read_html(wanted: dict[str, str], seconds: float) -> str | None:
                    handle = page.wait_for_function(
                        READ_PAGE_JS, arg=wanted, timeout=seconds * 1000, polling=250
                    )
                    try:
                        value = handle.json_value()
                    finally:
                        handle.dispose()
                    return value if isinstance(value, str) else None

                stage = "render"
                found = True
                try:
                    body = read_html(probe, RENDER_WAIT_SECONDS)
                except playwright_timeout:
                    found = False
                    # #region agent log
                    _mark("watch_timeout")
                    # #endregion
                    stage = "read"
                    try:
                        body = read_html({"needle": "", "selector": "", "must": ""}, 10)
                    except playwright_timeout:
                        log("fetch failed: the page never finished loading")
                        return None, "fetch failed"
                # #region agent log
                _mark("read")
                # #endregion
                final_path = urllib.parse.urlparse(page.url).path
                # #region agent log
                _agent_debug(
                    "I",
                    "watchpage.py:render_page",
                    "render finished",
                    {
                        "found": found,
                        "elapsed_ms": int((datetime.now().timestamp() - started) * 1000),
                        "body_len": len(body or ""),
                        "kept_path": stay["path"],
                        "final_path": final_path[:120],
                        "blocked": stay["blocked"],
                        "documents": stay["documents"],
                        "must_contain": must_contain,
                        "must_contain_seen": bool(must_contain)
                        and must_contain.casefold() in (body or "").casefold(),
                        "url_id_seen": url.rstrip("/").split("/")[-1].split("?")[0]
                        in (body or ""),
                        "watch_value": watch["value"] if watch else None,
                        "watch_seen": bool(watch)
                        and watch["value"].casefold() in (body or "").casefold(),
                        "marks_ms": _marks,
                        "routes": {k: v for k, v in _routes.items() if k != "last"},
                    },
                )
                # #endregion
                if not body:
                    log("fetch failed: the page never finished loading")
                    return None, "fetch failed"
                if stay["path"] is not None and final_path != stay["path"]:
                    log(f"fetch failed: the page moved to {final_path}")
                    return None, "fetch failed"
                if save_cookies and jar is not None and len(body) >= MIN_BODY_LENGTH:
                    store_playwright_cookies(jar, context.cookies())
                    save_cookie_jar(jar)
            except Exception as exc:
                failure = exc
                raise
            finally:
                stage_at_close = stage
                try:
                    browser.close()
                except Exception as close_exc:
                    # #region agent log
                    _agent_debug(
                        "D",
                        "watchpage.py:render_page",
                        "browser.close failed",
                        {
                            "stage": stage_at_close,
                            "close_type": type(close_exc).__name__,
                            "close_error": str(close_exc)[:500],
                            "had_failure": failure is not None,
                        },
                    )
                    # #endregion
                    if failure is None:
                        raise
    except SystemExit:
        raise
    except Exception as exc:
        message = str(exc)
        # #region agent log
        _agent_debug(
            "E",
            "watchpage.py:render_page",
            "render failed",
            {"stage": stage, "type": type(exc).__name__, "error": message[:800]},
        )
        # #endregion
        if "Executable doesn't exist" in message or "playwright install" in message:
            raise SystemExit(
                "Headless Chromium is not installed. Run ./setup.sh and answer yes when asked about headless Chromium."
            ) from exc
        log(f"fetch failed at {stage}: {type(exc).__name__}: {message}")
        return None, "fetch failed"

    if len(body) < MIN_BODY_LENGTH:
        log(f"page too short ({len(body)} bytes); skipping")
        return None, "page too short"
    return body, None


def fetch_page(
    url: str,
    *,
    render_javascript: bool = False,
    cookies_file: Path | None = None,
    user_agent: str | None = None,
    save_cookies: bool = False,
    watch: dict[str, str] | None = None,
    must_contain: str = "",
    headers: dict[str, str] | None = None,
    min_length: int = MIN_BODY_LENGTH,
) -> tuple[str | None, str | None]:
    """Return (body, None) or (None, reason)."""
    agent = user_agent or USER_AGENT
    jar = load_cookie_jar(Path(cookies_file)) if cookies_file else None
    if render_javascript:
        return render_page(
            url,
            user_agent=agent,
            jar=jar,
            save_cookies=save_cookies and jar is not None,
            watch=watch,
            must_contain=must_contain,
            headers=headers,
        )
    request = urllib.request.Request(url, headers={"User-Agent": agent, **(headers or {})})
    try:
        if jar is None:
            response_cm = urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS)
        else:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            response_cm = opener.open(request, timeout=FETCH_TIMEOUT_SECONDS)
        with response_cm as response:
            status = response.status
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        exc.close()
        log(f"fetch failed: HTTP {exc.code}")
        return None, "fetch failed"
    except Exception as exc:
        log(f"fetch failed: {exc}")
        return None, "fetch failed"

    if status != 200:
        log(f"fetch failed: HTTP {status}")
        return None, "fetch failed"
    if len(body) < min_length:
        log(f"page too short ({len(body)} bytes); skipping")
        return None, "page too short"
    if save_cookies and jar is not None:
        save_cookie_jar(jar)
    return body, None


def fetch_configured_page(
    config: dict[str, object], *, save_cookies: bool
) -> tuple[str | None, str | None]:
    cookies_file = config.get("cookies_file")
    user_agent = config.get("user_agent")
    watch = config.get("watch")
    is_json = isinstance(watch, dict) and watch.get("kind") == "json"
    headers = config.get("headers")
    return fetch_page(
        str(config.get("fetch_url") or config["page_url"]),
        render_javascript=bool(config["render_javascript"]),
        cookies_file=cookies_file if isinstance(cookies_file, Path) else None,
        user_agent=str(user_agent) if user_agent else None,
        save_cookies=save_cookies,
        watch=watch if isinstance(watch, dict) else None,
        must_contain=str(config.get("must_contain") or ""),
        headers=headers if isinstance(headers, dict) else None,
        # A JSON reply can be a few bytes. Parsing it is the real check.
        min_length=2 if is_json else MIN_BODY_LENGTH,
    )


def json_values(body: str, watch: dict[str, str]) -> tuple[bool, list[object]]:
    """Return (any JSON parsed, every value at watch["path"]).

    The JSON is the whole body, or the text of each element matching
    watch["script"], such as script#__NEXT_DATA__ or
    script[type="application/ld+json"]. "*" in the path matches every item
    of a list or object.
    """
    script = watch.get("script")
    if script:
        tags = import_beautifulsoup()(body, "html.parser").select(script)
        sources = [tag.get_text() for tag in tags]
    else:
        sources = [body]
    parsed = False
    found: list[object] = []
    for source in sources:
        try:
            current = [json.loads(source)]
        except ValueError:
            continue
        parsed = True
        for part in watch["path"].split("."):
            following: list[object] = []
            for item in current:
                if part == "*" and isinstance(item, list):
                    following.extend(item)
                elif part == "*" and isinstance(item, dict):
                    following.extend(item.values())
                elif isinstance(item, dict) and part in item:
                    following.append(item[part])
                elif isinstance(item, list) and part.isdigit() and int(part) < len(item):
                    following.append(item[int(part)])
            current = following
        found.extend(current)
    return parsed, found


def json_text(value: object) -> str:
    """JSON true is "true", null is "null", and a string is itself."""
    return value if isinstance(value, str) else json.dumps(value)


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
    """Return triggered, waiting, unexpected, or no_value."""
    must = str(config.get("must_contain") or "")
    if must and must.casefold() not in body.casefold():
        return "unexpected"
    watch = config["watch"]
    if not isinstance(watch, dict):
        raise SystemExit("Config watch must be an object.")
    if watch["kind"] == "json":
        parsed, values = json_values(body, watch)
        # Nothing at the path can only mean "not there yet" for a present
        # watch. For an absent watch it could be a changed reply, so it never
        # alerts.
        if not parsed or (not values and watch["alert_when"] == "absent"):
            return "no_value"
        wanted = watch["value"].casefold()
        matched = any(json_text(item).casefold() == wanted for item in values)
    else:
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
    if status == "no_value":
        watch = config["watch"]
        path = watch.get("path") if isinstance(watch, dict) else ""
        return "not_checked", f"no JSON value at {path}"
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


def _validate_schedule(schedule: str) -> str:
    if not isinstance(schedule, str):
        raise SystemExit(
            "A schedule is five cron fields, such as * * * * * for every minute "
            "or 0 * * * * for every hour."
        )
    fields = schedule.split()
    field_re = re.compile(r"[0-9A-Za-z*,/-]+")
    if len(fields) != 5 or any(field_re.fullmatch(field) is None for field in fields):
        raise SystemExit(
            "A schedule is five cron fields, such as * * * * * for every minute "
            "or 0 * * * * for every hour."
        )
    return " ".join(fields)


def _validate_cron_marker(marker: str) -> str:
    if not isinstance(marker, str):
        raise SystemExit("cron_marker must be one line and must not contain #")
    marker = marker.strip()
    if not marker or any(char in marker for char in "\r\n#"):
        raise SystemExit("cron_marker must be one line and must not contain #")
    return marker


def _cron_safe_text(text: str) -> str:
    if any(char in text for char in "\r\n#"):
        raise SystemExit(f"path cannot contain # or a newline: {text}")
    return text


def cron_line_state(line: str, marker: str) -> str | None:
    """Return active, commented, or None for this marker.

    A commented-out job starts with #. The marker is read only from the
    comment, using the same token rule as comment_out_cron.
    """
    marker = _validate_cron_marker(marker)
    if comment_contains_marker(line, marker):
        return "active"
    stripped = line.lstrip(" \t")
    if not stripped.startswith("#"):
        return None
    body = stripped[1:]
    if body.startswith(" "):
        body = body[1:]
    if comment_contains_marker(body, marker):
        return "commented"
    return None


def _read_user_crontab() -> list[str]:
    try:
        listed = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "crontab is not installed; add the scheduled job by hand"
        ) from exc
    if listed.returncode != 0:
        if "no crontab" in listed.stderr.lower():
            return []
        detail = listed.stderr.strip() or "could not read crontab"
        raise SystemExit(detail)
    return listed.stdout.splitlines()


def _write_user_crontab(lines: list[str]) -> None:
    result = subprocess.run(
        ["crontab", "-"],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "could not update crontab"
        raise SystemExit(f"could not update crontab: {detail}")


def cron_job_state(marker: str) -> tuple[str, list[str]]:
    """Return active, commented, or missing, plus the matching lines."""
    marker = _validate_cron_marker(marker)
    return _classify_crontab(_read_user_crontab(), marker)


def _uncomment_cron_line(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in " \t":
        index += 1
    if index >= len(line) or line[index] != "#":
        return line
    rest = line[index + 1 :]
    if rest.startswith(" "):
        rest = rest[1:]
    return line[:index] + rest


def build_cron_line(
    schedule: str,
    root: Path,
    config_path: Path,
    name: str,
    marker: str,
    flock_bin: str | None,
) -> str:
    """One crontab line. The marker sits in the comment so it can be turned off."""
    schedule = _validate_schedule(schedule)
    marker = _validate_cron_marker(marker)
    if not NAME_RE.fullmatch(name):
        raise SystemExit(
            "name must be letters, digits, dots, hyphens, or underscores"
        )
    root = root.resolve()
    config_path = config_path.resolve()
    command: list[str] = []
    if flock_bin:
        lock_path = root / f"{name}.lock"
        command.append(shlex.quote(_cron_safe_text(flock_bin)))
        command.append("-n")
        command.append(shlex.quote(_cron_safe_text(str(lock_path))))
    python_bin = root / ".venv" / "bin" / "python"
    script = root / "watchpage.py"
    log_path = root / f"{name}.log"
    command.extend(
        [
            shlex.quote(_cron_safe_text(str(python_bin))),
            shlex.quote(_cron_safe_text(str(script))),
            "--config",
            shlex.quote(_cron_safe_text(str(config_path))),
        ]
    )
    quoted_log = shlex.quote(_cron_safe_text(str(log_path)))
    return f"{schedule} {' '.join(command)} >> {quoted_log} 2>&1 # {marker}"


def _classify_crontab(lines: list[str], marker: str) -> tuple[str, list[str]]:
    active: list[str] = []
    commented: list[str] = []
    for line in lines:
        state = cron_line_state(line, marker)
        if state == "active":
            active.append(line)
        elif state == "commented":
            commented.append(line)
    if active:
        return "active", active
    if commented:
        return "commented", commented
    return "missing", []


def install_cron_job(marker: str, line: str) -> str:
    """Append the watcher line when that marker is not already in crontab.

    Returns installed, active, or commented. An active or commented line is
    left as it is, so a second setup does not add a duplicate job.
    """
    if in_test_mode():
        raise SystemExit("refusing to edit crontab during a test")
    marker = _validate_cron_marker(marker)
    lines = _read_user_crontab()
    state, _matches = _classify_crontab(lines, marker)
    if state != "missing":
        return state
    lines.append(line)
    _write_user_crontab(lines)
    return "installed"


def uncomment_cron_job(marker: str) -> str:
    """Remove the leading hash from lines this marker comments out.

    Returns uncommented, active, or missing. Other crontab lines stay.
    """
    if in_test_mode():
        raise SystemExit("refusing to edit crontab during a test")
    marker = _validate_cron_marker(marker)
    lines = _read_user_crontab()
    state, _matches = _classify_crontab(lines, marker)
    if state != "commented":
        return state
    updated = [
        _uncomment_cron_line(line) if cron_line_state(line, marker) == "commented" else line
        for line in lines
    ]
    _write_user_crontab(updated)
    return "uncommented"


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
    body, problem = fetch_configured_page(config, save_cookies=False)
    outcome, detail = assess(body, problem, config)
    if outcome == "not_checked":
        log(f"watch not checked: {detail}")
        return 1
    if outcome == "triggered":
        log("watch triggered")
        return 0
    log("watch not triggered")
    return 0


def import_watch_cookies(config: dict[str, object], args: argparse.Namespace) -> int:
    destination = config.get("cookies_file")
    destination_dir = None
    hint_root = None
    if not isinstance(destination, Path):
        destination = None
        destination_dir = ROOT / "cookies"
        hint_root = ROOT
    host = urllib.parse.urlparse(str(config["page_url"])).hostname or ""
    return cookie_import.import_site_cookies(
        destination=destination if isinstance(destination, Path) else None,
        destination_dir=destination_dir,
        hint_root=hint_root,
        suggest_domain=host,
        config_hint=None,
        browser=args.browser,
        domain=args.domain,
        profile=args.profile,
    )


def import_cookies(args: argparse.Namespace) -> int:
    """Import one domain without a watch config."""
    return cookie_import.import_site_cookies(
        destination=None,
        destination_dir=ROOT / "cookies",
        hint_root=ROOT,
        suggest_domain="",
        config_hint=None,
        browser=args.browser,
        domain=args.domain,
        profile=args.profile,
    )


def main(argv: list[str] | None = None) -> int:
    global STATE_PATH
    parser = argparse.ArgumentParser(
        description="Watch an HTTP page and text when the configured condition is met."
    )
    parser.add_argument(
        "--config",
        help="JSON file with the page URL and what to watch. Optional with --import-cookies.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch the page and report whether the watch is triggered. Does not send texts, change state, or update cookies.",
    )
    parser.add_argument(
        "--import-cookies",
        action="store_true",
        help="Copy cookies for one domain from one local browser into cookies/{domain}.cookie. Does not fetch the page. --config is optional.",
    )
    parser.add_argument(
        "--browser",
        choices=cookie_import.BROWSERS,
        help="Browser to import from. Required when there is no terminal.",
    )
    parser.add_argument(
        "--domain",
        help="Single domain to import, such as www.example.com. Required when there is no terminal.",
    )
    parser.add_argument(
        "--profile",
        help="Browser profile to import from. Required when that browser has several profiles and there is no terminal.",
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
    args = parser.parse_args(argv)
    if args.import_cookies and (args.dry_run or args.test_sms or args.no_record):
        raise SystemExit(
            "--import-cookies cannot be combined with --dry-run, --test-sms, or --no-record"
        )
    if args.dry_run and (args.test_sms or args.no_record):
        raise SystemExit("--dry-run cannot be combined with other test flags")
    if args.import_cookies and not args.config:
        return import_cookies(args)
    if not args.config:
        parser.error("the following arguments are required: --config")

    watch_config = load_watch_config(
        Path(args.config), require_cookies_file=not args.import_cookies
    )
    if args.import_cookies:
        return import_watch_cookies(watch_config, args)

    STATE_PATH = Path(watch_config["state_file"])

    if args.dry_run:
        return run_dry(watch_config)

    warn_config()
    config = load_config()
    config.update(watch_config)
    state = load_state()
    recipients = list(config["recipients"])

    if args.test_sms:
        log("sending test SMS")
        return send_pending(config, state, record=False)

    if not args.no_record and finish_if_complete(config, state):
        return 0

    body, problem = fetch_configured_page(config, save_cookies=True)
    if body is None:
        if args.no_record:
            return 0
        return note_fetch_failure(config, state)
    if not args.no_record:
        clear_fetch_failures(state)

    outcome, detail = assess(body, problem, config)
    if outcome == "not_triggered":
        log("still waiting")
        return 0
    if outcome == "not_checked":
        log(f"{detail}; skipping")
        return 0

    log("watch triggered")
    return send_pending(config, state, record=not args.no_record)


if __name__ == "__main__":
    sys.exit(main())

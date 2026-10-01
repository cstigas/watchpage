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
        "message": message,
        "must_contain": must_contain,
        "cron_marker": cron_marker,
        "state_file": state_file,
        "cookies_file": cookies_file,
        "user_agent": user_agent,
        "render_javascript": render_javascript,
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
) -> tuple[str | None, str | None]:
    """Return the HTML after scripts run, using a headless browser."""
    sync_playwright, playwright_timeout = import_playwright()
    agent = user_agent or USER_AGENT
    log("rendering page in a headless browser")
    stage = "start"
    # #region agent log
    _agent_debug(
        "A",
        "watchpage.py:render_page",
        "render start",
        {
            "has_cookies": jar is not None,
            "cookie_count": len(list(jar)) if jar is not None else 0,
            "user_agent_set": bool(user_agent),
        },
    )
    # #endregion
    try:
        with sync_playwright() as playwright:
            stage = "launch"
            browser = playwright.chromium.launch(headless=True)
            # #region agent log
            _agent_debug(
                "A",
                "watchpage.py:render_page",
                "browser launched",
                {"browser_type": type(browser).__name__},
            )
            # #endregion
            failure = None
            try:
                context = None
                if jar is None:
                    stage = "new_page"
                    page = browser.new_page(user_agent=agent)
                else:
                    stage = "new_context"
                    context = browser.new_context(user_agent=agent)
                    payload = playwright_cookie_payload(jar, url)
                    # #region agent log
                    _agent_debug(
                        "C",
                        "watchpage.py:render_page",
                        "cookie payload ready",
                        {
                            "payload_count": len(payload),
                            "missing_domain": sum(
                                1 for item in payload if not item.get("domain")
                            ),
                            "expired": sum(
                                1
                                for item in payload
                                if isinstance(item.get("expires"), int)
                                and item["expires"] < int(datetime.now().timestamp())
                            ),
                        },
                    )
                    # #endregion
                    if payload:
                        stage = "add_cookies"
                        try:
                            context.add_cookies(payload)
                        except Exception as cookie_exc:
                            # #region agent log
                            _agent_debug(
                                "C",
                                "watchpage.py:render_page",
                                "add_cookies failed",
                                {
                                    "type": type(cookie_exc).__name__,
                                    "error": str(cookie_exc)[:500],
                                },
                            )
                            # #endregion
                            log("fetch failed: could not apply cookies")
                            return None, "fetch failed"
                    stage = "new_page"
                    page = context.new_page()
                stage = "goto"
                # #region agent log
                _agent_debug(
                    "B",
                    "watchpage.py:render_page",
                    "before goto",
                    {"stage": stage},
                )
                # #endregion
                response = page.goto(
                    url,
                    wait_until="load",
                    timeout=FETCH_TIMEOUT_SECONDS * 1000,
                )
                # #region agent log
                _agent_debug(
                    "B",
                    "watchpage.py:render_page",
                    "after goto",
                    {"status": None if response is None else response.status},
                )
                # #endregion
                if response is None:
                    log("fetch failed: no response")
                    return None, "fetch failed"
                if response.status != 200:
                    log(f"fetch failed: HTTP {response.status}")
                    return None, "fetch failed"
                try:
                    stage = "networkidle"
                    page.wait_for_load_state("networkidle", timeout=5000)
                except playwright_timeout:
                    pass
                stage = "content"
                body = page.content()
                if (
                    save_cookies
                    and jar is not None
                    and context is not None
                    and len(body) >= MIN_BODY_LENGTH
                ):
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
                            "failure_type": (
                                type(failure).__name__ if failure is not None else None
                            ),
                            "failure_error": (
                                str(failure)[:500] if failure is not None else None
                            ),
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
            {
                "stage": stage,
                "type": type(exc).__name__,
                "error": message[:800],
                "cause": (
                    str(exc.__cause__)[:400] if exc.__cause__ is not None else None
                ),
            },
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
        )
    request = urllib.request.Request(url, headers={"User-Agent": agent})
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
    if len(body) < MIN_BODY_LENGTH:
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
    return fetch_page(
        str(config["page_url"]),
        render_javascript=bool(config["render_javascript"]),
        cookies_file=cookies_file if isinstance(cookies_file, Path) else None,
        user_agent=str(user_agent) if user_agent else None,
        save_cookies=save_cookies,
    )


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

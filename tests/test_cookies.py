"""Cookie import reads one domain, and fetches send only that jar."""

import argparse
import json
import os
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

import cookie_import
import watchpage

KEPT = "kept-secret"
DOT = "dot-secret"
SESSION = "session-secret"
WWW = "www-secret"
OTHER = "other-secret"
EXPIRED = "expired-secret"
PLAIN = "plain-secret"
SECURE = "super-secret-secure"
REFRESHED = "refreshed-token"


def encrypt_v10(value: str, key: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    encryptor = cipher.encryptor()
    return b"v10" + encryptor.update(padded) + encryptor.finalize()


def write_firefox_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    rows = [
        ("example.com", "kept", KEPT, "/", now + 3600, 0, 0),
        (".example.com", "dot", DOT, "/", now + 3600, 0, 0),
        ("example.com", "session", SESSION, "/", 0, 0, 0),
        ("www.example.com", "www", WWW, "/", now + 3600, 0, 0),
        ("other.test", "other", OTHER, "/", now + 3600, 0, 0),
        ("example.com", "old", EXPIRED, "/", 1, 0, 0),
    ]
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE moz_cookies (
            host TEXT,
            name TEXT,
            value TEXT,
            path TEXT,
            expiry INTEGER,
            isSecure INTEGER,
            isHttpOnly INTEGER
        )
        """
    )
    connection.executemany(
        "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()


def firefox_ini(root: Path) -> None:
    (root / "profiles.ini").write_text(
        "\n".join(
            [
                "[Profile0]",
                "Name=default",
                "IsRelative=1",
                "Path=abc.default",
                "Default=1",
                "[Profile1]",
                "Name=other",
                "IsRelative=1",
                "Path=other.default",
                "Default=0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def safari_record(domain: str, name: str, value: str, expires_unix: int | None) -> bytes:
    domain_b = domain.encode() + b"\x00"
    name_b = name.encode() + b"\x00"
    path_b = b"/\x00"
    value_b = value.encode() + b"\x00"
    strings_at = 56
    header = bytearray(strings_at)
    struct.pack_into("<I", header, 16, strings_at)
    struct.pack_into("<I", header, 20, strings_at + len(domain_b))
    struct.pack_into("<I", header, 24, strings_at + len(domain_b) + len(name_b))
    struct.pack_into(
        "<I", header, 28, strings_at + len(domain_b) + len(name_b) + len(path_b)
    )
    body = domain_b + name_b + path_b + value_b
    mac_expiry = 0.0 if expires_unix is None else float(expires_unix - 978307200)
    record = header + body + struct.pack("<dd", 0.0, mac_expiry)
    struct.pack_into("<I", record, 0, len(record))
    return bytes(record)


def safari_file(records: list[bytes]) -> bytes:
    header_size = 8 + 4 * len(records)
    body = bytearray()
    offsets = []
    cursor = header_size
    for record in records:
        offsets.append(cursor)
        body += record
        cursor += len(record)
    page = bytearray(b"\x00\x00\x01\x00")
    page += struct.pack("<I", len(records))
    page += b"".join(struct.pack("<I", offset) for offset in offsets)
    page += body
    blob = bytearray(b"cook")
    blob += struct.pack(">I", 1)
    blob += struct.pack(">I", len(page))
    blob += page
    return bytes(blob)


class Handler(BaseHTTPRequestHandler):
    seen: list[dict[str, str | None]] = []

    def do_GET(self):
        Handler.seen.append(
            {
                "cookie": self.headers.get("Cookie"),
                "agent": self.headers.get("User-Agent"),
                "path": self.path,
            }
        )
        if self.path.startswith("/fail"):
            self.send_response(500)
            self.end_headers()
            return
        body = ("summer concert tickets will be available. " * 30).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Set-Cookie", f"refreshed={REFRESHED}; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class CookieImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "firefox"
        profile = self.root / "abc.default"
        other = self.root / "other.default"
        write_firefox_db(profile / "cookies.sqlite")
        write_firefox_db(other / "cookies.sqlite")
        firefox_ini(self.root)
        self.original_root = cookie_import.firefox_root
        cookie_import.firefox_root = lambda: self.root
        self.opened: list[Path] = []
        self.original_copy = cookie_import._sqlite_copy

        @contextmanager
        def recording_copy(source: Path):
            self.opened.append(source)
            with self.original_copy(source) as copy:
                yield copy

        cookie_import._sqlite_copy = recording_copy

    def tearDown(self):
        cookie_import.firefox_root = self.original_root
        cookie_import._sqlite_copy = self.original_copy

    def test_domain_must_be_one_hostname(self):
        self.assertEqual(cookie_import.normalize_domain("Example.COM"), "example.com")
        self.assertEqual(cookie_import.normalize_domain(".example.com"), "example.com")
        for bad in (
            "https://example.com",
            "example.com:443",
            "example.com/path",
            "*.example.com",
            "ex ample.com",
            "",
        ):
            with self.assertRaises(SystemExit):
                cookie_import.normalize_domain(bad)

    def test_firefox_import_keeps_one_domain(self):
        destination = self.tmp / "cookies.txt"
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cookie_import.import_site_cookies(
                destination=destination,
                suggest_domain="www.example.com",
                config_hint="cookies/shop-cart.txt",
                browser="firefox",
                domain="example.com",
                profile="default",
                interactive=False,
            )
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Imported 3 cookies for example.com from firefox", text)
        self.assertIn(str(destination), text)
        self.assertIn('"cookies_file": "cookies/shop-cart.txt"', text)
        for secret in (KEPT, DOT, SESSION, WWW, OTHER, EXPIRED):
            self.assertNotIn(secret, text)
        saved = destination.read_text(encoding="utf-8")
        self.assertIn(KEPT, saved)
        self.assertIn(DOT, saved)
        self.assertIn(SESSION, saved)
        self.assertNotIn(WWW, saved)
        self.assertNotIn(OTHER, saved)
        self.assertNotIn(EXPIRED, saved)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.opened, [self.root / "abc.default" / "cookies.sqlite"])

    def test_several_profiles_need_a_choice_before_open(self):
        with self.assertRaises(SystemExit) as caught:
            cookie_import.import_site_cookies(
                destination=self.tmp / "cookies.txt",
                suggest_domain="example.com",
                config_hint=None,
                browser="firefox",
                domain="example.com",
                profile=None,
                interactive=False,
            )
        self.assertIn("--profile", str(caught.exception.code))
        self.assertEqual(self.opened, [])

    def test_noninteractive_exits_before_opening_a_database(self):
        with self.assertRaises(SystemExit) as caught:
            cookie_import.import_site_cookies(
                destination=self.tmp / "cookies.txt",
                suggest_domain="example.com",
                config_hint=None,
                browser=None,
                domain=None,
                profile=None,
                interactive=False,
            )
        message = str(caught.exception.code)
        self.assertIn("--browser", message)
        self.assertIn("--domain", message)
        self.assertEqual(self.opened, [])

        with self.assertRaises(SystemExit):
            cookie_import.import_site_cookies(
                destination=self.tmp / "cookies.txt",
                suggest_domain="example.com",
                config_hint=None,
                browser="firefox",
                domain=None,
                profile=None,
                interactive=False,
            )
        self.assertEqual(self.opened, [])

    def test_chrome_v10_round_trip(self):
        key = cookie_import.chrome_key("peanuts", platform="linux")
        encrypted = encrypt_v10("session-secret-value", key)
        self.assertEqual(
            cookie_import.decrypt_chrome_value(encrypted, key), "session-secret-value"
        )
        with self.assertRaises(SystemExit) as caught:
            cookie_import.decrypt_chrome_value(b"v11" + b"\x00" * 24, key)
        self.assertIn("Netscape cookies.txt", str(caught.exception.code))

    def test_chrome_query_does_not_decrypt_other_domains(self):
        key = cookie_import.chrome_key("test-password", platform="linux")
        expires = int((time.time() + 3600 + cookie_import._WINDOWS_TO_UNIX) * 1_000_000)
        path = self.tmp / "Cookies"
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                path TEXT,
                expires_utc INTEGER,
                is_secure INTEGER,
                is_httponly INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("example.com", "session", "", encrypt_v10(KEPT, key), "/", expires, 0, 0),
                ("other.test", "nope", "", b"v11" + b"\x00" * 24, "/", expires, 0, 0),
            ],
        )
        connection.commit()
        connection.close()
        cookies = cookie_import.read_chrome_cookies(path, "example.com", key)
        self.assertEqual([cookie.value for cookie in cookies], [KEPT])

        connection = sqlite3.connect(path)
        connection.execute("DELETE FROM cookies")
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("example.com", "session", "", b"v11" + b"\x00" * 24, "/", expires, 0, 0),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(SystemExit) as caught:
            cookie_import.read_chrome_cookies(path, "example.com", key)
        self.assertIn("Netscape cookies.txt", str(caught.exception.code))

    def test_safari_parser_keeps_one_domain(self):
        future = int(time.time()) + 3600
        blob = safari_file(
            [
                safari_record("example.com", "kept", KEPT, future),
                safari_record(".example.com", "dot", DOT, future),
                safari_record("www.example.com", "www", WWW, future),
            ]
        )
        path = self.tmp / "Cookies.binarycookies"
        path.write_bytes(blob)
        cookies = cookie_import.read_safari_cookies(path, "example.com")
        values = {cookie.value for cookie in cookies}
        self.assertEqual(values, {KEPT, DOT})


class CookieFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        Handler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.cookies = self.tmp / "jar.txt"
        later = int(time.time()) + 3600
        cookie_import.write_netscape(
            self.cookies,
            [
                cookie_import.make_cookie(
                    name="session",
                    value=PLAIN,
                    host="127.0.0.1",
                    path="/",
                    secure=False,
                    http_only=False,
                    expires=later,
                ),
                cookie_import.make_cookie(
                    name="locked",
                    value=SECURE,
                    host="127.0.0.1",
                    path="/",
                    secure=True,
                    http_only=False,
                    expires=later,
                ),
                cookie_import.make_cookie(
                    name="other",
                    value=OTHER,
                    host="example.com",
                    path="/",
                    secure=False,
                    http_only=False,
                    expires=later,
                ),
            ],
        )
        self.before = self.cookies.read_bytes()
        self.saved_env = {key: os.environ.get(key) for key in watchpage.REQUIRED_SETTINGS}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_fetch_sends_matching_cookies_and_can_save(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            body, problem = watchpage.fetch_page(
                self.url,
                cookies_file=self.cookies,
                user_agent="TicketsBrowser/9",
                save_cookies=False,
            )
        self.assertIsNone(problem)
        self.assertGreaterEqual(len(body or ""), watchpage.MIN_BODY_LENGTH)
        self.assertEqual(self.cookies.read_bytes(), self.before)
        header = Handler.seen[-1]["cookie"] or ""
        self.assertIn(PLAIN, header)
        self.assertNotIn(SECURE, header)
        self.assertNotIn(OTHER, header)
        self.assertEqual(Handler.seen[-1]["agent"], "TicketsBrowser/9")
        for secret in (PLAIN, SECURE, OTHER, REFRESHED):
            self.assertNotIn(secret, stdout.getvalue())

        with redirect_stdout(stdout):
            body, problem = watchpage.fetch_page(
                self.url, cookies_file=self.cookies, save_cookies=True
            )
        self.assertIsNone(problem)
        saved = self.cookies.read_text(encoding="utf-8")
        self.assertIn(REFRESHED, saved)
        self.assertIn(PLAIN, saved)
        self.assertEqual(self.cookies.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(REFRESHED, stdout.getvalue())

    def test_failed_fetch_does_not_save_cookies(self):
        _body, problem = watchpage.fetch_page(
            self.url + "fail", cookies_file=self.cookies, save_cookies=True
        )
        self.assertEqual(problem, "fetch failed")
        self.assertEqual(self.cookies.read_bytes(), self.before)

    def test_dry_run_does_not_update_cookies(self):
        config = self.write_watch()
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = watchpage.main(["--config", str(config), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.cookies.read_bytes(), self.before)
        self.assertNotIn(PLAIN, stdout.getvalue())

    def test_waiting_run_saves_cookies_without_sending(self):
        for key, value in {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_FROM_NUMBER": "+14165550100",
            "OUTAGE_TO_NUMBER": "+14165550199",
        }.items():
            os.environ[key] = value
        config = self.write_watch()

        def reject_sms(*_args, **_kwargs):
            raise AssertionError("waiting run must not send SMS")

        original = watchpage.send_sms
        watchpage.send_sms = reject_sms
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = watchpage.main(["--config", str(config)])
        finally:
            watchpage.send_sms = original
        self.assertEqual(code, 0)
        self.assertIn(REFRESHED, self.cookies.read_text(encoding="utf-8"))
        self.assertNotIn(PLAIN, stdout.getvalue())
        self.assertNotIn(REFRESHED, stdout.getvalue())

    def test_import_cannot_be_combined_with_dry_run(self):
        with self.assertRaises(SystemExit) as caught:
            watchpage.main(["--import-cookies", "--dry-run", "--config", "missing.json"])
        self.assertIn("cannot be combined", str(caught.exception))

    def test_main_without_a_terminal_does_not_open_cookies(self):
        config = self.write_watch()
        original_stdin = cookie_import.sys.stdin
        opened = []
        original_read = cookie_import.read_domain_cookies

        class Closed:
            def isatty(self):
                return False

        def reject_read(*_args, **_kwargs):
            opened.append(1)
            raise AssertionError("cookie database was opened")

        cookie_import.sys.stdin = Closed()
        cookie_import.read_domain_cookies = reject_read
        try:
            with self.assertRaises(SystemExit) as caught:
                watchpage.main(["--config", str(config), "--import-cookies"])
        finally:
            cookie_import.sys.stdin = original_stdin
            cookie_import.read_domain_cookies = original_read
        self.assertIn("--browser", str(caught.exception.code))
        self.assertEqual(opened, [])

    def write_watch(self, **overrides) -> Path:
        data = {
            "name": "shop-cart",
            "url": self.url,
            "to_numbers": ["+14165550101"],
            "must_contain": "summer concert",
            "state_file": str(self.tmp / "state.json"),
            "cookies_file": str(self.cookies),
            "user_agent": "TicketsBrowser/9",
            "watch": {
                "kind": "text",
                "value": "will be available",
                "alert_when": "absent",
            },
        }
        data.update(overrides)
        path = self.tmp / "watch.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class CookieCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "firefox"
        profile = self.root / "abc.default"
        write_firefox_db(profile / "cookies.sqlite")
        (self.root / "profiles.ini").write_text(
            "\n".join(
                [
                    "[Profile0]",
                    "Name=default",
                    "IsRelative=1",
                    "Path=abc.default",
                    "Default=1",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.original_root = cookie_import.firefox_root
        cookie_import.firefox_root = lambda: self.root
        self.saved_env = {key: os.environ.get(key) for key in watchpage.REQUIRED_SETTINGS}
        for key in watchpage.REQUIRED_SETTINGS:
            os.environ.pop(key, None)

    def tearDown(self):
        cookie_import.firefox_root = self.original_root
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_import_command_does_not_fetch(self):
        config = self.tmp / "watch.json"
        config.write_text(
            json.dumps(
                {
                    "name": "shop-cart",
                    "url": "https://example.com/tickets",
                    "to_numbers": ["+14165550101"],
                    "cookies_file": str(self.tmp / "imported.txt"),
                    "watch": {
                        "kind": "text",
                        "value": "add to cart",
                        "alert_when": "present",
                    },
                }
            ),
            encoding="utf-8",
        )
        original_fetch = watchpage.fetch_page

        def reject_fetch(*_args, **_kwargs):
            raise AssertionError("import must not fetch")

        watchpage.fetch_page = reject_fetch
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = watchpage.main(
                    [
                        "--config",
                        str(config),
                        "--import-cookies",
                        "--browser",
                        "firefox",
                        "--domain",
                        "example.com",
                        "--profile",
                        "default",
                    ]
                )
        finally:
            watchpage.fetch_page = original_fetch
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("example.com", text)
        self.assertIn("firefox", text)
        self.assertNotIn(KEPT, text)
        self.assertNotIn("Add this to the watch config", text)

    def test_hint_when_the_config_has_no_cookies_file(self):
        saved_root = watchpage.ROOT
        watchpage.ROOT = self.tmp
        try:
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = watchpage.import_watch_cookies(
                    {
                        "name": "shop-cart",
                        "page_url": "https://www.example.com/tickets",
                        "cookies_file": None,
                    },
                    argparse.Namespace(
                        browser="firefox",
                        domain="example.com",
                        profile="default",
                    ),
                )
        finally:
            watchpage.ROOT = saved_root
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn('"cookies_file": "cookies/shop-cart.txt"', text)
        self.assertNotIn(KEPT, text)
        self.assertTrue((self.tmp / "cookies" / "shop-cart.txt").is_file())


if __name__ == "__main__":
    unittest.main()

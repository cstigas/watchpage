"""Copy one domain's cookies from one local browser into a Netscape file.

A run asks for a single browser and a single domain, then opens that
profile's cookie database only. Cron never calls this module.
"""

from __future__ import annotations

import configparser
import hashlib
import http.cookiejar
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

BROWSERS = ("chrome", "chromium", "brave", "edge", "firefox", "safari")

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")

CHROME_DECRYPT_ERROR = (
    "Could not decrypt these cookies (expected Chrome v10). "
    "Export a Netscape cookies.txt and set cookies_file to that path."
)

_WINDOWS_TO_UNIX = 11_644_473_600
_MAC_TO_UNIX = 978_307_200

CHROME_FAMILY = {
    "chrome": {
        "darwin": "Library/Application Support/Google/Chrome",
        "linux": ".config/google-chrome",
        "service": "Chrome Safe Storage",
        "account": "Chrome",
    },
    "chromium": {
        "darwin": "Library/Application Support/Chromium",
        "linux": ".config/chromium",
        "service": "Chromium Safe Storage",
        "account": "Chromium",
    },
    "brave": {
        "darwin": "Library/Application Support/BraveSoftware/Brave-Browser",
        "linux": ".config/BraveSoftware/Brave-Browser",
        "service": "Brave Safe Storage",
        "account": "Brave",
    },
    "edge": {
        "darwin": "Library/Application Support/Microsoft Edge",
        "linux": ".config/microsoft-edge",
        "service": "Microsoft Edge Safe Storage",
        "account": "Microsoft Edge",
    },
}

_SKIP_CHROME_DIRS = {
    "System Profile",
    "Guest Profile",
    "Crash Reports",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
}


class Profile:
    def __init__(self, name: str, cookie_file: Path, is_default: bool) -> None:
        self.name = name
        self.cookie_file = cookie_file
        self.is_default = is_default


def home() -> Path:
    return Path.home()


def normalize_domain(raw: str) -> str:
    """Return one hostname, or exit if the input is not a single domain."""
    text = raw.strip().lower()
    if text.startswith("."):
        text = text[1:]
    if not text or len(text) > 253 or not DOMAIN_RE.fullmatch(text):
        raise SystemExit(
            "Domain must be a single hostname such as www.example.com, "
            f"got {raw!r}"
        )
    return text


def host_matches(stored: str, domain: str) -> bool:
    """True for the domain itself and the same host with a leading dot."""
    host = stored.strip().lower()
    if host.startswith("."):
        host = host[1:]
    return host == domain


def make_cookie(
    *,
    name: str,
    value: str,
    host: str,
    path: str,
    secure: bool,
    http_only: bool,
    expires: int | None,
) -> http.cookiejar.Cookie:
    initial_dot = host.startswith(".")
    rest = {"HTTPOnly": ""} if http_only else {}
    return http.cookiejar.Cookie(
        0,
        name,
        value,
        None,
        False,
        host,
        initial_dot,
        initial_dot,
        path or "/",
        False,
        secure,
        expires,
        expires is None,
        None,
        None,
        rest,
    )


def write_netscape(path: Path, cookies: list[http.cookiejar.Cookie]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jar = http.cookiejar.MozillaCookieJar(str(path))
    for cookie in cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True, ignore_expires=False)
    os.chmod(path, 0o600)


def _reject_netscape_value(domain: str, name: str, value: str) -> None:
    if any(char in name or char in value for char in "\t\r\n"):
        raise SystemExit(f"A cookie for {domain} cannot be stored in a Netscape file.")


def firefox_root() -> Path:
    if sys.platform == "darwin":
        return home() / "Library/Application Support/Firefox"
    return home() / ".mozilla/firefox"


def _chrome_root(browser: str) -> Path:
    spec = CHROME_FAMILY[browser]
    relative = spec["darwin"] if sys.platform == "darwin" else spec["linux"]
    return home() / relative


def _chrome_cookie_file(profile_dir: Path) -> Path | None:
    network = profile_dir / "Network" / "Cookies"
    legacy = profile_dir / "Cookies"
    if network.is_file():
        return network
    if legacy.is_file():
        return legacy
    return None


def _safari_cookie_file() -> Path | None:
    candidates = (
        home()
        / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
        home() / "Library/Cookies/Cookies.binarycookies",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def list_profiles(browser: str) -> list[Profile]:
    """Profile names whose cookie file exists. Does not open those files."""
    if browser == "safari":
        if sys.platform != "darwin":
            return []
        path = _safari_cookie_file()
        if path is None:
            return []
        return [Profile("Default", path, True)]

    if browser == "firefox":
        root = firefox_root()
        ini = root / "profiles.ini"
        profiles: list[Profile] = []
        if not ini.is_file():
            return profiles
        parser = configparser.ConfigParser()
        parser.read(ini)
        for section in parser.sections():
            if not section.lower().startswith("profile"):
                continue
            name = parser.get(section, "Name", fallback=section)
            raw_path = parser.get(section, "Path", fallback="")
            if not raw_path:
                continue
            is_relative = parser.get(section, "IsRelative", fallback="1") == "1"
            is_default = parser.get(section, "Default", fallback="0") == "1"
            profile_dir = (root / raw_path) if is_relative else Path(raw_path)
            cookies = profile_dir / "cookies.sqlite"
            if cookies.is_file():
                profiles.append(Profile(name, cookies, is_default))
        return profiles

    if browser not in CHROME_FAMILY:
        return []
    root = _chrome_root(browser)
    if not root.is_dir():
        return []
    profiles = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or child.name in _SKIP_CHROME_DIRS or child.name.startswith("."):
            continue
        cookies = _chrome_cookie_file(child)
        if cookies is not None:
            profiles.append(Profile(child.name, cookies, child.name == "Default"))
    return profiles


def chrome_key(password: str, *, platform: str | None = None) -> bytes:
    system = sys.platform if platform is None else platform
    iterations = 1003 if system == "darwin" else 1
    return hashlib.pbkdf2_hmac(
        "sha1",
        password.encode("utf-8"),
        b"saltysalt",
        iterations,
        dklen=16,
    )


def decrypt_chrome_value(encrypted: bytes, key: bytes) -> str:
    if not encrypted.startswith(b"v10"):
        raise SystemExit(_chrome_decrypt_error())
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise SystemExit(
            "Chrome cookie import needs the cryptography package. Run ./setup.sh"
        ) from exc
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
        decryptor = cipher.decryptor()
        padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
        return _decode_chrome_plaintext(raw)
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(_chrome_decrypt_error()) from exc


def _decode_chrome_plaintext(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if len(raw) > 32:
            try:
                return raw[32:].decode("utf-8")
            except UnicodeDecodeError:
                pass
        raise SystemExit(_chrome_decrypt_error())


def _chrome_decrypt_error() -> str:
    message = CHROME_DECRYPT_ERROR
    if sys.platform.startswith("linux"):
        message += " On Linux, install secretstorage if the browser key is in the keyring."
    return message


def chrome_expiry_to_unix(expires_utc: int) -> int | None:
    if expires_utc <= 0:
        return None
    return int(expires_utc / 1_000_000 - _WINDOWS_TO_UNIX)


def read_firefox_cookies(
    db_path: Path, domain: str, *, now: float | None = None
) -> list[http.cookiejar.Cookie]:
    moment = time.time() if now is None else now
    rows = _query_sqlite(
        db_path,
        """
        SELECT host, name, value, path, expiry, isSecure, isHttpOnly
        FROM moz_cookies
        WHERE lower(host) = ? OR lower(host) = ?
        """,
        (domain, "." + domain),
        domain,
        "Firefox",
    )
    cookies = []
    for host, name, value, path, expiry, secure, http_only in rows:
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not host_matches(str(host), domain):
            continue
        _reject_netscape_value(domain, name, value)
        try:
            expiry_int = int(expiry)
        except (TypeError, ValueError):
            continue
        if expiry_int != 0 and expiry_int <= moment:
            continue
        cookies.append(
            make_cookie(
                name=name,
                value=value,
                host=str(host),
                path=str(path or "/"),
                secure=bool(secure),
                http_only=bool(http_only),
                expires=None if expiry_int == 0 else expiry_int,
            )
        )
    return cookies


def read_chrome_cookies(
    db_path: Path,
    domain: str,
    key: bytes,
    *,
    now: float | None = None,
) -> list[http.cookiejar.Cookie]:
    moment = time.time() if now is None else now
    rows = _query_sqlite(
        db_path,
        """
        SELECT host_key, name, value, encrypted_value, path, expires_utc,
               is_secure, is_httponly
        FROM cookies
        WHERE lower(host_key) = ? OR lower(host_key) = ?
        """,
        (domain, "." + domain),
        domain,
        "Chrome",
    )
    cookies = []
    for host, name, plain, encrypted, path, expires_utc, secure, http_only in rows:
        if not isinstance(name, str) or not host_matches(str(host), domain):
            continue
        try:
            expires = chrome_expiry_to_unix(int(expires_utc))
        except (TypeError, ValueError):
            continue
        if expires is not None and expires <= moment:
            continue
        if encrypted:
            value = decrypt_chrome_value(bytes(encrypted), key)
        elif isinstance(plain, str) and plain:
            value = plain
        else:
            continue
        _reject_netscape_value(domain, name, value)
        cookies.append(
            make_cookie(
                name=name,
                value=value,
                host=str(host),
                path=str(path or "/"),
                secure=bool(secure),
                http_only=bool(http_only),
                expires=expires,
            )
        )
    return cookies


def read_safari_cookies(
    path: Path, domain: str, *, now: float | None = None
) -> list[http.cookiejar.Cookie]:
    moment = time.time() if now is None else now
    try:
        data = path.read_bytes()
    except PermissionError as exc:
        raise SystemExit(
            "Cannot read Safari cookies. Grant Full Disk Access to this terminal and try again."
        ) from exc
    except OSError as exc:
        raise SystemExit("Could not read Safari cookies.") from exc
    try:
        records = _safari_records(data, domain)
    except Exception as exc:
        raise SystemExit("Could not read Safari cookies.") from exc
    cookies = []
    for record in records:
        expires = record["expires"]
        if not isinstance(expires, int) and expires is not None:
            continue
        if expires is not None and expires <= moment:
            continue
        _reject_netscape_value(domain, str(record["name"]), str(record["value"]))
        cookies.append(
            make_cookie(
                name=str(record["name"]),
                value=str(record["value"]),
                host=str(record["host"]),
                path=str(record["path"]),
                secure=bool(record["secure"]),
                http_only=bool(record["http_only"]),
                expires=expires,
            )
        )
    return cookies


def _safari_records(data: bytes, domain: str) -> list[dict[str, object]]:
    if len(data) < 8 or data[:4] != b"cook":
        raise ValueError("not a Safari cookie file")
    page_count = struct.unpack_from(">I", data, 4)[0]
    sizes_end = 8 + 4 * page_count
    if page_count < 1 or sizes_end > len(data):
        raise ValueError("bad Safari cookie header")
    page_sizes = struct.unpack_from(f">{page_count}I", data, 8)
    offset = sizes_end
    records: list[dict[str, object]] = []
    for page_size in page_sizes:
        page = data[offset : offset + page_size]
        offset += page_size
        if len(page) < 8:
            continue
        count = struct.unpack_from("<I", page, 4)[0]
        for index in range(count):
            start = 8 + index * 4
            if start + 4 > len(page):
                break
            cookie_at = struct.unpack_from("<I", page, start)[0]
            if cookie_at + 4 > len(page):
                continue
            cookie_size = struct.unpack_from("<I", page, cookie_at)[0]
            record = page[cookie_at : cookie_at + cookie_size]
            parsed = _parse_safari_cookie(record, domain)
            if parsed is not None:
                records.append(parsed)
    return records


def _parse_safari_cookie(record: bytes, domain: str) -> dict[str, object] | None:
    if len(record) < 48:
        return None
    flags = struct.unpack_from("<I", record, 8)[0]
    domain_at = struct.unpack_from("<I", record, 16)[0]
    name_at = struct.unpack_from("<I", record, 20)[0]
    path_at = struct.unpack_from("<I", record, 24)[0]
    value_at = struct.unpack_from("<I", record, 28)[0]
    host = _c_string(record, domain_at)
    if host is None or not host_matches(host, domain):
        return None
    # Expiration is a Mac absolute time at offset 40, before the strings.
    # The tail of the record is the cookie value.
    raw_expiry = struct.unpack_from("<d", record, 40)[0]
    if raw_expiry == 0:
        expires: int | None = None
    else:
        expires = int(raw_expiry + _MAC_TO_UNIX)
    name = _c_string(record, name_at)
    path = _c_string(record, path_at)
    value = _c_string(record, value_at)
    if not name or value is None or path is None:
        return None
    return {
        "host": host,
        "name": name,
        "value": value,
        "path": path or "/",
        "secure": bool(flags & 1),
        "http_only": bool(flags & 4),
        "expires": expires,
    }


def _c_string(buf: bytes, offset: int) -> str | None:
    if offset < 0 or offset >= len(buf):
        return None
    end = buf.find(b"\x00", offset)
    if end < 0:
        return None
    return buf[offset:end].decode("utf-8", errors="replace")


def _query_sqlite(
    db_path: Path,
    sql: str,
    params: tuple[str, str],
    domain: str,
    label: str,
) -> list[tuple]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SystemExit(f"Could not read {label} cookies for {domain}.") from exc
    try:
        return list(connection.execute(sql, params))
    except sqlite3.Error as exc:
        raise SystemExit(f"Could not read {label} cookies for {domain}.") from exc
    finally:
        connection.close()


@contextmanager
def _sqlite_copy(source: Path):
    temporary = tempfile.TemporaryDirectory(prefix="watchpage-cookies-")
    try:
        dest = Path(temporary.name) / source.name
        shutil.copy2(source, dest)
        for suffix in ("-wal", "-shm"):
            extra = Path(str(source) + suffix)
            if extra.is_file():
                shutil.copy2(extra, Path(str(dest) + suffix))
        yield dest
    finally:
        temporary.cleanup()


def _mac_safe_storage_password(service: str, account: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", service, "-a", account],
        capture_output=True,
        text=True,
        check=False,
    )
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        raise SystemExit(
            "Could not read the browser Safe Storage password from Keychain. "
            "Allow the prompt, or export a Netscape cookies.txt and set cookies_file to that path."
        )
    return password


def _linux_keyring_password(label: str) -> str | None:
    try:
        import secretstorage
    except ImportError:
        return None
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            collection.unlock()
        for item in collection.get_all_items():
            if item.get_label() == label:
                secret = item.get_secret()
                if isinstance(secret, bytes):
                    return secret.decode("utf-8")
                return str(secret)
    except Exception:
        return None
    return None


def chrome_decryption_key(browser: str) -> bytes:
    spec = CHROME_FAMILY[browser]
    if sys.platform == "darwin":
        password = _mac_safe_storage_password(spec["service"], spec["account"])
    else:
        password = _linux_keyring_password(spec["service"]) or "peanuts"
    return chrome_key(password)


def read_domain_cookies(browser: str, profile: Profile, domain: str) -> list[http.cookiejar.Cookie]:
    """Open one profile cookie file and return unexpired cookies for one domain."""
    try:
        if browser == "firefox":
            with _sqlite_copy(profile.cookie_file) as copy:
                return read_firefox_cookies(copy, domain)
        if browser == "safari":
            with _sqlite_copy(profile.cookie_file) as copy:
                return read_safari_cookies(copy, domain)
        key = chrome_decryption_key(browser)
        with _sqlite_copy(profile.cookie_file) as copy:
            return read_chrome_cookies(copy, domain, key)
    except PermissionError as exc:
        if browser == "safari":
            raise SystemExit(
                "Cannot read Safari cookies. Grant Full Disk Access to this terminal and try again."
            ) from exc
        raise SystemExit(f"Cannot read the {browser} cookie database.") from exc


def _prompt_choice(question: str, options: list[str], default: str | None = None) -> str:
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option}")
    while True:
        hint = f" [{default}]" if default else ""
        try:
            answer = input(f"{question}{hint}: ").strip()
        except EOFError:
            raise SystemExit(_noninteractive_message()) from None
        if not answer and default:
            return default
        if answer in options:
            return answer
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(options):
                return options[number - 1]
        print("Enter a number from the list.")


def _prompt_domain(suggested: str) -> str:
    try:
        default: str | None = normalize_domain(suggested)
    except SystemExit:
        default = None
    print(
        "Cookies are imported for this domain only. "
        "A cookie set on a parent domain is not included unless you enter that domain."
    )
    while True:
        hint = f" [{default}]" if default else ""
        try:
            answer = input(f"Domain to import{hint}: ").strip()
        except EOFError:
            raise SystemExit(_noninteractive_message()) from None
        if not answer and default:
            return default
        try:
            return normalize_domain(answer)
        except SystemExit as exc:
            print(exc.code, file=sys.stderr)


def _noninteractive_message() -> str:
    return (
        "Pass --browser and --domain when there is no terminal. "
        "Example: --import-cookies --browser chrome --domain example.com"
    )


def _choose_profile(browser: str, requested: str | None, interactive: bool) -> Profile:
    profiles = list_profiles(browser)
    if not profiles:
        raise SystemExit(f"{browser} has no cookie database on this machine.")
    if requested:
        for profile in profiles:
            if profile.name == requested:
                return profile
        raise SystemExit(f"No {browser} profile named {requested!r}.")
    if len(profiles) == 1:
        return profiles[0]
    if not interactive:
        names = ", ".join(profile.name for profile in profiles)
        raise SystemExit(
            f"{browser} has more than one profile ({names}). Pass --profile to choose one."
        )
    default = next((profile.name for profile in profiles if profile.is_default), None)
    chosen = _prompt_choice(
        "Import cookies from which profile?",
        [profile.name for profile in profiles],
        default,
    )
    for profile in profiles:
        if profile.name == chosen:
            return profile
    raise SystemExit(f"No {browser} profile named {chosen!r}.")


def import_site_cookies(
    *,
    destination: Path | None,
    suggest_domain: str,
    config_hint: str | None,
    browser: str | None,
    domain: str | None,
    profile: str | None,
    interactive: bool | None = None,
    destination_dir: Path | None = None,
    hint_root: Path | None = None,
) -> int:
    """Write one domain from one browser profile to destination. Returns 0."""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and (not browser or not domain):
        raise SystemExit(_noninteractive_message())
    if sys.platform not in ("darwin", "linux"):
        raise SystemExit("Cookie import runs on macOS and Linux.")

    if browser is None:
        choices = [name for name in BROWSERS if list_profiles(name)]
        if not choices:
            raise SystemExit("No browser cookie database was found.")
        browser = _prompt_choice("Import cookies from which browser?", choices)
    if browser not in BROWSERS:
        raise SystemExit(f"Browser must be one of: {', '.join(BROWSERS)}")
    if browser == "safari" and sys.platform != "darwin":
        raise SystemExit("Safari cookies are only available on macOS.")

    if domain is None:
        domain = _prompt_domain(suggest_domain)
    else:
        domain = normalize_domain(domain)

    if destination is None:
        if destination_dir is None:
            raise SystemExit("No destination for the cookie file.")
        destination = destination_dir / f"{domain}.cookie"
        if config_hint is None and hint_root is not None:
            config_hint = destination.relative_to(hint_root).as_posix()

    chosen = _choose_profile(browser, profile.strip() if profile else None, interactive)
    cookies = read_domain_cookies(browser, chosen, domain)
    write_netscape(destination, cookies)
    noun = "cookie" if len(cookies) == 1 else "cookies"
    print(f"Imported {len(cookies)} {noun} for {domain} from {browser}")
    print(f"Wrote {destination}")
    if config_hint is not None:
        print(f'Add this to the watch config: "cookies_file": "{config_hint}"')
    return 0

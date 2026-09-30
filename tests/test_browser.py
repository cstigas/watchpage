"""Headless Chromium is used only when a watch asks for JavaScript rendering."""

import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import watchpage

ROOT = Path(__file__).resolve().parents[1]
PAD = "x" * 500
PAGE = f"""<!DOCTYPE html>
<html><head><title>tickets</title></head>
<body>
<p id="status">will be available {PAD}</p>
<script>
var parts = ["rendered", "by", "javascript"];
document.getElementById("status").textContent = parts.join(" ") + " {PAD}";
</script>
</body></html>
"""


def process_tree_rss_kb(root_pid: int) -> int:
    """Resident memory, in KB, of a process and every process it started."""
    listing = subprocess.run(
        ["ps", "-ax", "-o", "pid=", "-o", "ppid=", "-o", "rss="],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return 0
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, ppid, size = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rss[pid] = size
        children.setdefault(ppid, []).append(pid)
    total = 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class HeadlessBrowserTest(unittest.TestCase):
    def test_plain_fetch_does_not_import_playwright(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "import watchpage\n"
                'watchpage.fetch_page("http://127.0.0.1:1/", render_javascript=False)\n'
                "raise SystemExit('playwright' in sys.modules)\n",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("rendering page in a headless browser", result.stdout)

    def test_headless_browser_sees_text_added_by_javascript(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        peak_kb = 0
        stop = threading.Event()

        def sample_memory() -> None:
            nonlocal peak_kb
            while not stop.is_set():
                peak_kb = max(peak_kb, process_tree_rss_kb(os.getpid()))
                stop.wait(0.05)

        sampler = threading.Thread(target=sample_memory, daemon=True)
        sampler.start()
        try:
            plain, plain_problem = watchpage.fetch_page(url, render_javascript=False)
            self.assertIsNone(plain_problem)
            self.assertIn("will be available", plain)
            self.assertNotIn("rendered by javascript", plain)

            rendered, problem = watchpage.fetch_page(url, render_javascript=True)
            self.assertIsNone(problem, rendered)
            self.assertIn("rendered by javascript", rendered)
        finally:
            stop.set()
            sampler.join()
            peak_kb = max(peak_kb, process_tree_rss_kb(os.getpid()))
            print(f"peak resident memory: {peak_kb / 1024:.0f} MB", flush=True)
            server.shutdown()
            server.server_close()

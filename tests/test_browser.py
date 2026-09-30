"""Headless Chromium is used only when a watch asks for JavaScript rendering."""

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
        try:
            plain, plain_problem = watchpage.fetch_page(url, render_javascript=False)
            self.assertIsNone(plain_problem)
            self.assertIn("will be available", plain)
            self.assertNotIn("rendered by javascript", plain)

            rendered, problem = watchpage.fetch_page(url, render_javascript=True)
            self.assertIsNone(problem, rendered)
            self.assertIn("rendered by javascript", rendered)
        finally:
            server.shutdown()
            server.server_close()

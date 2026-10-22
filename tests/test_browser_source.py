"""Optional real-browser checks; set VIZSENTRY_BROWSER_TESTS=1 after installing Chromium."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading

import pytest

from vizsentry.sources import SourceService

pytestmark = pytest.mark.skipif(os.environ.get("VIZSENTRY_BROWSER_TESTS") != "1", reason="Chromium integration opt-in")


class FixtureHandler(BaseHTTPRequestHandler):
    redirect_target = ""
    observed = []

    def do_GET(self):
        self.observed.append((self.server.server_port, self.path, self.headers.get("X-Api-Key")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_target)
            self.end_headers()
            return
        content_type = "text/html; charset=utf-8"
        if self.path == "/login":
            body = b'<html><body><input type="password"></body></html>'
        elif self.path == "/raw":
            content_type = "text/plain; charset=utf-8"
            body = "真实日志\nerror: timeout\n".encode()
        elif self.path == "/image":
            if "sid=fixture" not in self.headers.get("Cookie", ""):
                self.send_error(401)
                return
            content_type = "image/png"
            body = b"fixture-image-bytes"
        else:
            body = b'''<html><body><div id="log">first log page</div>
            <button id="next" onclick="document.querySelector('#log').innerText='second log page';this.disabled=true;">Next</button>
            </body></html>'''
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "sid=fixture; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_worker_logs_pagination_login_cookie_download_and_multiplex(tmp_path, monkeypatch):
    monkeypatch.setenv("VIZSENTRY_BROWSER_HEADLESS", "1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    other = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    other_thread = threading.Thread(target=other.serve_forever, daemon=True)
    other_thread.start()
    FixtureHandler.observed = []
    FixtureHandler.redirect_target = f"http://127.0.0.1:{other.server_port}/raw"
    url = f"http://127.0.0.1:{server.server_port}"
    async def execute():
        source = SourceService(tmp_path)
        try:
            raw, html = await asyncio.gather(
                source.browser_log(url + "/raw", {}, tmp_path / "raw"),
                source.browser_log(url + "/html", {}, tmp_path / "html"),
            )
            assert raw["status"] == "complete"
            assert Path(raw["raw_path"]).read_text(encoding="utf8").startswith("真实日志")
            assert html["status"] == "partial" and Path(html["raw_path"]).is_file()
            paged = await source.browser_log(url + "/html", {"body_selector": "#log", "next_selector": "#next"}, tmp_path / "paged")
            assert paged["status"] == "complete"
            assert Path(paged["text_path"]).read_text() == "first log page\n\nsecond log page"
            assert len(paged["raw_paths"]) == 3  # original HTTP response + two rendered snapshots
            missing = await source.browser_log(url + "/html", {"next_selector": "#not-present"}, tmp_path / "missing-next")
            assert missing["status"] == "partial"
            redirected = await source.browser_log(url + "/redirect", {"header": "X-Api-Key", "token": "test-secret"}, tmp_path / "redirected")
            assert redirected["status"] == "complete"
            assert all(token is None for port, _, token in FixtureHandler.observed if port == other.server_port)
            assert any(token == "test-secret" for port, route, token in FixtureHandler.observed if route == "/redirect")
            login = await source.browser_log(url + "/login", {}, tmp_path / "login")
            assert login["status"] == "login_required"
            image_path = tmp_path / "image.png"
            await source.download_image({"url": url + "/image"}, image_path, {"mode": "browser", "url": url + "/html"})
            assert image_path.read_bytes() == b"fixture-image-bytes"
        finally:
            await source.close()
    try:
        asyncio.run(execute())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        other.shutdown()
        other.server_close()
        other_thread.join(timeout=5)

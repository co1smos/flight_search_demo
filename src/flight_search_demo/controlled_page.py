from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from typing import Tuple


PAGE_TITLE = "Controlled Browser Stack Test"


class _ControlledPageServer(ThreadingHTTPServer):
    daemon_threads = True


class ControlledPageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/", "/index.html"}:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        page = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(PAGE_TITLE)}</title>
  </head>
  <body>
    <main>
      <h1>{html.escape(PAGE_TITLE)}</h1>
      <p id="instructions">Enter a marker, save it, and read the persisted value from local storage.</p>
      <p id="handoff-ready"><strong>Human takeover ready</strong></p>
      <label for="marker-input">Marker</label>
      <input id="marker-input" name="marker" />
      <button id="save-marker" type="button">Save marker to this browser profile</button>
      <p id="marker-status">Persisted marker: <strong id="marker-value">none</strong></p>
      <a id="offsite-link" href="https://example.com/">Attempt offsite navigation</a>
    </main>
    <script>
      const input = document.getElementById("marker-input");
      const markerValue = document.getElementById("marker-value");
      const storageKey = "controlled-page-marker";
      const cookieName = "controlled-page-marker";

      function readCookie() {{
        const prefix = `${{cookieName}}=`;
        const parts = document.cookie.split(";").map((part) => part.trim());
        const match = parts.find((part) => part.startsWith(prefix));
        return match ? decodeURIComponent(match.slice(prefix.length)) : "";
      }}

      function refresh() {{
        const value = readCookie() || window.localStorage.getItem(storageKey) || "none";
        markerValue.textContent = value;
        input.value = value === "none" ? "" : value;
      }}

      document.getElementById("save-marker").addEventListener("click", () => {{
        const value = input.value.trim();
        if (!value) {{
          return;
        }}
        window.localStorage.setItem(storageKey, value);
        document.cookie = `${{cookieName}}=${{encodeURIComponent(value)}}; path=/; SameSite=Lax`;
        refresh();
      }});

      refresh();
    </script>
  </body>
</html>"""
        encoded = page.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class ControlledPageServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self._server = _ControlledPageServer((host, port), ControlledPageHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def address(self) -> Tuple[str, int]:
        return self._server.server_address

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

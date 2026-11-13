"""Loopback static frontend + fixed local API proxy; no production reverse proxy."""

import http.client
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from policy import API_PORT, APP_SOURCE, PACKAGE, ROOT, WEB_PORT, config, deny_external_network
from uat_policy import allowed

DIST = ROOT / "source/frontend/dist"
BANNER = f'<aside style="position:fixed;bottom:0;right:0;z-index:99999;background:#542;color:white;padding:6px">ARM2 UAT B1 | TEST | source {APP_SOURCE[:7]}</aside>'


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, body, content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-ARM2-UAT", PACKAGE)
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        for key, value in headers:
            if key.lower() in {"set-cookie", "location"}:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        path = unquote(urlsplit(self.path).path)
        if path.startswith("/api/"):
            if not allowed(self.command, path):
                return self.respond(404, b'{"detail":"Outside native UAT scope"}')
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                return self.respond(413, b'{"detail":"Body too large"}')
            body = self.rfile.read(length) if length else None
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() in {"content-type", "cookie", "x-csrf-token", "origin", "user-agent", "host"}}
            connection = http.client.HTTPConnection("127.0.0.1", API_PORT, timeout=20)
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                return self.respond(response.status, response.read(), response.getheader("Content-Type", "application/json"), response.getheaders())
            except OSError:
                return self.respond(503, b'{"detail":"Native API unavailable"}')
            finally:
                connection.close()
        if self.command != "GET":
            return self.respond(405, b'{"detail":"GET only"}')
        if path == "/uat-instance.json":
            return self.respond(200, json.dumps({"package": PACKAGE, "source_sha": APP_SOURCE, "instance_id": config()["instance_id"]}).encode())
        target = (DIST / path.lstrip("/")).resolve()
        if not target.is_relative_to(DIST.resolve()):
            return self.respond(403, b'{"detail":"Path outside frontend"}')
        if not target.is_file():
            if Path(path).suffix:
                return self.respond(404, b'{"detail":"Asset missing"}')
            target = DIST / "index.html"
        data = target.read_bytes()
        if target.name == "index.html":
            data = data.replace(b"</body>", (BANNER + "</body>").encode())
        return self.respond(200, data, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = dispatch


def main():
    config()
    deny_external_network()
    server = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

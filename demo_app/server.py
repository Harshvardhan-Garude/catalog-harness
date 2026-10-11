"""Small single-process HTTP demo. No inference routes or directory serving."""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from catalog_harness.storage import ROOT, connect
from .data import DemoRepository

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
FILES["/static/style.css"] = FILES["/style.css"]
FILES["/static/app.js"] = FILES["/app.js"]


def demo_connection():
    """Hosted environments must explicitly select the existing event database.

    In particular, a host's automatically provisioned MONGODB_URI must not
    silently replace the event database. Local setup keeps its existing .env.
    """
    hosted = bool(os.environ.get("PORT")) or os.environ.get("CATALOG_HOSTED") == "1"
    if hosted:
        names = ("CATALOG_MONGODB_URI", "CATALOG_MONGODB_DATABASE")
        if any(not os.environ.get(name, "").strip() for name in names):
            raise ValueError("Hosted Atlas configuration missing: CATALOG_MONGODB_URI, CATALOG_MONGODB_DATABASE")
        os.environ["MONGODB_URI"] = os.environ["CATALOG_MONGODB_URI"]
        os.environ["MONGODB_DATABASE"] = os.environ["CATALOG_MONGODB_DATABASE"]
    return connect()


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, repository=None, repository_factory=None):
        self.repository = repository
        self.repository_error = None
        self.repository_lock = threading.Lock()
        if repository is None:
            try:
                self.repository = (repository_factory or
                                   (lambda: DemoRepository(connector=demo_connection)))()
            except Exception:
                self.repository_error = ("Saved results unavailable. Check Atlas connectivity and configuration, "
                                         "or restore the complete local artifacts/enrichment-v1 saved report. "
                                         "Hosted startup requires CATALOG_MONGODB_URI and CATALOG_MONGODB_DATABASE "
                                         "for the existing event database.")
        super().__init__(address, Handler)

    def server_close(self):
        if self.repository is not None:
            self.repository.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "CatalogHarnessDemo"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Request paths and user-controlled headers are never logged.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in FILES:
            name, content_type = FILES[path]
            try:
                self._send(200, (STATIC / name).read_bytes(), content_type)
            except OSError:
                self._error(503, "Demo interface files are unavailable")
        elif path == "/healthz":
            self._send(200, {"status": "ok", "saved_results_available": self.server.repository is not None})
        elif path == "/api/state":
            if self.server.repository is None:
                return self._error(503, self.server.repository_error)
            try:
                with self.server.repository_lock:
                    state = self.server.repository.state()
                self._send(200, state)
            except Exception:
                self._error(503, "Saved demo data or store profiles are unavailable or invalid")
        else:
            self._error(404, "Not found")

    def _same_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        host = self.headers.get("Host", "")
        public = os.environ.get("SITE_URL") or os.environ.get("ROOT_URL")
        allowed = {"http://" + host, "https://" + host}
        if public:
            p = urlsplit(public)
            allowed.add(p.scheme + "://" + p.netloc)
        return parsed.scheme + "://" + parsed.netloc in allowed and parsed.path in ("", "/")

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/placement/preview", "/api/placement/accept"):
            return self._error(404, "Not found")
        if not self._same_origin():
            return self._error(403, "Placement requests must originate from this demo")
        if self.server.repository is None:
            return self._error(503, self.server.repository_error)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._error(400, "Provide a bounded JSON request")
            body = json.loads(self.rfile.read(size).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError, OSError):
            return self._error(400, "Invalid JSON request")
        try:
            with self.server.repository_lock:
                if path.endswith("/preview"):
                    if set(body) - {"store_id", "source_record_ids"}:
                        return self._error(400, "Unknown preview fields")
                    result = self.server.repository.preview(body.get("store_id"), body.get("source_record_ids"))
                else:
                    if set(body) - {"preview_id", "source_record_ids"}:
                        return self._error(400, "Unknown acceptance fields")
                    result = self.server.repository.accept(body.get("preview_id"), body.get("source_record_ids"))
            self._send(200, result)
        except ValueError as error:
            # Repository ValueErrors are fixed validation messages, never driver errors.
            self._error(400, str(error)[:500] or "Invalid placement request")
        except Exception:
            self._error(503, "Atlas placement operation unavailable; no external store was changed")


def main():
    parser = argparse.ArgumentParser(description="Browse saved Catalog Harness results; never calls a model")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = DemoServer((args.host, args.port))
    print("Catalog Harness saved-results demo: http://{}:{}".format(args.host, args.port), flush=True)
    if server.repository is None:
        print(server.repository_error, flush=True)
    else:
        print("Saved results source: " + server.repository.source["kind"], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Small single-process HTTP demo. No inference routes or directory serving."""

import argparse
import json
import os
import threading
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from pymongo.errors import PyMongoError

from catalog_harness.storage import ROOT, connect
from enrichment.atlas_diagnostics import sanitized_error
from .data import DemoRepository
from .benchmark_view import catalog_page, saved_report

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8")}
FILES["/static/style.css"] = FILES["/style.css"]
FILES["/static/app.js"] = FILES["/app.js"]


def missing_connection_variables():
    hosted = bool(os.environ.get("PORT")) or os.environ.get("CATALOG_HOSTED") == "1"
    names = ("CATALOG_MONGODB_URI", "CATALOG_MONGODB_DATABASE") if hosted else ("MONGODB_URI", "MONGODB_DATABASE")
    values = {} if hosted else dotenv_values(ROOT / ".env")
    missing = []
    for name in names:
        value = (os.environ.get(name, values.get(name)) or "").strip()
        if not value or value.startswith("YOUR_"):
            missing.append(name)
    return missing


def demo_connection():
    """Hosted environments must explicitly select the existing event database.

    In particular, a host's automatically provisioned MONGODB_URI must not
    silently replace the event database. Local setup keeps its existing .env.
    """
    hosted = bool(os.environ.get("PORT")) or os.environ.get("CATALOG_HOSTED") == "1"
    if hosted:
        if missing_connection_variables():
            raise ValueError("Hosted Atlas configuration is missing")
        os.environ["MONGODB_URI"] = os.environ["CATALOG_MONGODB_URI"]
        os.environ["MONGODB_DATABASE"] = os.environ["CATALOG_MONGODB_DATABASE"]
    return connect()


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, repository=None, repository_factory=None):
        self.repository = repository
        self.repository_error = None
        self.startup_error = None
        self.repository_lock = threading.Lock()
        if repository is None:
            try:
                self.repository = (repository_factory or
                                   (lambda: DemoRepository(connector=demo_connection,
                                                           on_atlas_error=self._record_startup_error)))()
            except Exception:
                if self.startup_error is None:
                    self._record_startup_error(ValueError())
                self.repository_error = ("Saved results unavailable. Check Atlas connectivity and configuration, "
                                         "or restore the complete local artifacts/enrichment-v1 saved report. "
                                         "Hosted startup requires CATALOG_MONGODB_URI and CATALOG_MONGODB_DATABASE "
                                         "for the existing event database.")
                self.repository_error += " Startup diagnostic: {}/{}.".format(
                    self.startup_error["category"], self.startup_error["code"])
                if self.startup_error.get("missing"):
                    self.repository_error += " Missing: " + ", ".join(self.startup_error["missing"]) + "."
        super().__init__(address, Handler)

    def _record_startup_error(self, error):
        missing = missing_connection_variables()
        if missing:
            self.startup_error = {"category": "configuration", "code": "missing_variables", "missing": missing}
        elif isinstance(error, PyMongoError):
            self.startup_error = sanitized_error(error)
        elif isinstance(error, ValueError):
            self.startup_error = {"category": "saved_data", "code": "verification_failed"}
        else:
            self.startup_error = sanitized_error(error)

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

    def _unavailable(self):
        self._send(503, {"error": self.server.repository_error, "diagnostic": self.server.startup_error})

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in FILES:
            name, content_type = FILES[path]
            try:
                self._send(200, (STATIC / name).read_bytes(), content_type)
            except OSError:
                self._error(503, "Demo interface files are unavailable")
        elif path == "/healthz":
            result = {"status": "ok", "saved_results_available": self.server.repository is not None}
            if self.server.startup_error is not None:
                result["startup_error"] = self.server.startup_error
            revision = os.environ.get("RENDER_GIT_COMMIT", "")
            if re.fullmatch(r"[0-9a-f]{40}", revision):
                result["git_commit"] = revision
            self._send(200, result)
        elif path == "/api/state":
            if self.server.repository is None:
                return self._unavailable()
            try:
                with self.server.repository_lock:
                    state = self.server.repository.state()
                self._send(200, state)
            except Exception:
                self._error(503, "Saved demo data or store profiles are unavailable or invalid")
        elif path in ("/api/benchmark", "/api/benchmark/catalog"):
            if self.server.repository is None:
                return self._unavailable()
            try:
                parameters = parse_qs(parsed.query, keep_blank_values=True)
                if path == "/api/benchmark":
                    if parameters:
                        return self._error(400, "Benchmark report accepts no query parameters")
                    result = saved_report(self.server.repository)
                else:
                    if (set(parameters) - {"dataset_id", "supplier", "status", "page"}
                            or any(len(values) != 1 for values in parameters.values())):
                        return self._error(400, "Invalid benchmark browser parameters")
                    result = catalog_page(self.server.repository, dataset_id=parameters.get("dataset_id", [""])[0],
                        supplier=parameters.get("supplier", [""])[0], status=parameters.get("status", [""])[0],
                        page=parameters.get("page", ["1"])[0])
                self._send(200, result)
            except ValueError as error:
                self._error(400, str(error)[:200])
            except Exception:
                self._error(503, "Atlas benchmark read unavailable; saved experiment results remain unchanged")
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
            return self._unavailable()
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

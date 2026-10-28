"""A stand-in for one tool on APIM that serves a given OpenAPI document verbatim.

``dev.mock_tool_server`` renders its export from a list of operations, so it cannot
produce the odd documents real exports can (path-item parameters, ``$ref`` parameters,
colliding operation ids). This one serves whatever document it is given and records
every other request.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from src.config import ToolBinding

TOOL_ID = "demo"


@dataclass
class Call:
    method: str
    path: str  # raw path as received (still percent-encoded)
    query: dict
    headers: dict
    body: object = None


@dataclass
class OpenApiStub:
    document: dict
    export_status: int = 200
    export_delay: float = 0.0
    export_requests: int = 0
    calls: list = field(default_factory=list)

    def start(self) -> "OpenApiStub":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                if parts.path == f"/tools/{TOOL_ID}/openapi.json":
                    outer.export_requests += 1
                    if outer.export_delay:
                        time.sleep(outer.export_delay)
                    status, payload = outer.export_status, outer.document
                else:
                    outer.calls.append(
                        Call(
                            self.command,
                            parts.path,
                            parse_qs(parts.query),
                            {k.lower(): v for k, v in self.headers.items()},
                            json.loads(raw) if raw else None,
                        )
                    )
                    status, payload = 200, {"ok": True}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, *_args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def binding(self, tool_id: str = TOOL_ID) -> ToolBinding:
        return ToolBinding(
            tool_id, ("read",), f"http://127.0.0.1:{self.port}/tools/{TOOL_ID}", "api://x/.default"
        )


def document(paths: dict, components: dict | None = None) -> dict:
    doc = {"openapi": "3.0.3", "info": {"title": "demo", "version": "1"}, "paths": paths}
    if components:
        doc["components"] = components
    return doc

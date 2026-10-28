"""Mock APIM tool server: for local runs of the agent container, and for the tests.

It serves the same two things APIM serves for a bound tool:

- ``GET /tools/<id>/openapi.json``: the tool's OpenAPI export (same document shape
  the platform's ``tool-export.sh`` produces, including ``x-required-role``)
- the tool's routes under ``/tools/<id>/...``

Standard library only, so it runs anywhere ``python`` does:

    python -m dev.mock_tool_server --port 8001

By default every request needs an ``Authorization: Bearer <anything>`` header and
is answered. With ``enforce_roles=True`` (used by the tests) the bearer token must
look like ``<anything>|roles=read,write`` and the role a route needs must be listed,
which mimics APIM's per-operation role check (HTTP 403 otherwise).
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

EXPORT_PATH = "/openapi.json"


@dataclass(frozen=True)
class Operation:
    method: str
    path: str  # relative to /tools/<id>, with {param} placeholders
    role: str
    description: str
    handler: Any  # (path_params: dict, body: Any) -> JSON-serialisable


@dataclass
class RecordedRequest:
    method: str
    path: str  # full path as received, e.g. /tools/mock-shop-api/products
    authorization: str | None
    body: Any = None


def operation_id(method: str, path: str) -> str:
    """Same derivation as scripts/lib/tool-export.sh in the infra repository."""
    text = f"{method}-{path}".lower().replace("{", "").replace("}", "")
    text = text.replace("/", "-").replace("_", "-")
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:80]


PRODUCTS = [
    {"id": "1", "name": "Rain jacket", "price": 89.99},
    {"id": "2", "name": "Denim jacket", "price": 59.99},
    {"id": "3", "name": "Wool scarf", "price": 19.99},
]


def _list_products(_params, _body):
    return PRODUCTS


def _get_product(params, _body):
    return next((p for p in PRODUCTS if p["id"] == params["id"]), {"error": "not found"})


def _add_basket_item(_params, body):
    return {"added": body or {}}


def _list_secrets(_params, _body):
    return [{"name": "launch-codes"}]


def default_tools() -> dict[str, list[Operation]]:
    return {
        "mock-shop-api": [
            Operation("GET", "/products", "read", "List products", _list_products),
            Operation("GET", "/products/{id}", "read", "Get one product", _get_product),
            Operation("POST", "/basket/items", "write", "Add an item to the basket", _add_basket_item),
        ],
        "other-api": [
            Operation("GET", "/secrets", "read", "List secrets", _list_secrets),
        ],
    }


def render_openapi(tool_id: str, base_url: str, operations: list[Operation]) -> dict:
    paths: dict[str, dict] = {}
    for op in operations:
        entry: dict[str, Any] = {
            "operationId": operation_id(op.method, op.path),
            "summary": op.description,
            "x-required-role": op.role,
            "responses": {
                "200": {"description": "Success"},
                "401": {"description": "Missing or invalid token"},
            },
        }
        params = re.findall(r"\{([^}]+)\}", op.path)
        if params:
            entry["parameters"] = [
                {"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                for p in params
            ]
        paths.setdefault(op.path, {})[op.method.lower()] = entry
    return {
        "openapi": "3.0.3",
        "info": {"title": tool_id, "version": "1.0.0"},
        "servers": [{"url": f"{base_url.rstrip('/')}/tools/{tool_id}"}],
        "paths": paths,
    }


def _roles_in(authorization: str | None) -> set[str] | None:
    """Roles carried by a test token, or None when there is no bearer token."""
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) <= 7:
        return None
    match = re.search(r"\|roles=([^|]*)", authorization)
    return set(filter(None, match.group(1).split(","))) if match else set()


@dataclass
class MockToolServer:
    tools: dict[str, list[Operation]] = field(default_factory=default_tools)
    enforce_roles: bool = False
    host: str = "127.0.0.1"
    port: int = 0
    requests: list[RecordedRequest] = field(default_factory=list)

    def start(self) -> "MockToolServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer._serve(self)

            def do_POST(self):  # noqa: N802
                outer._serve(self)

            def log_message(self, *_args):
                pass

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def requests_to(self, path_prefix: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.path.startswith(path_prefix)]

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        auth = handler.headers.get("Authorization")
        self.requests.append(RecordedRequest(handler.command, handler.path, auth, body))

        status, payload = self._respond(handler.command, handler.path.split("?")[0], auth, body)
        data = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _respond(self, method: str, path: str, auth: str | None, body: Any):
        match = re.fullmatch(r"/tools/([^/]+)(/.*)", path)
        if not match or match.group(1) not in self.tools:
            return 404, {"error": "unknown tool"}
        tool_id, rest = match.groups()
        operations = self.tools[tool_id]

        roles = _roles_in(auth)
        if roles is None:
            return 401, {"error": "missing bearer token"}

        if method == "GET" and rest == EXPORT_PATH:
            if self.enforce_roles and not roles & {op.role for op in operations}:
                return 403, {"error": "no role on this tool"}
            return 200, render_openapi(tool_id, self.base_url, operations)

        for op in operations:
            params = _match(op.path, rest)
            if op.method == method and params is not None:
                if self.enforce_roles and op.role not in roles:
                    return 403, {"error": f"role '{op.role}' required"}
                return 200, op.handler(params, body)
        return 404, {"error": "no such operation"}


def _match(template: str, path: str) -> dict[str, str] | None:
    pattern = re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", template)
    found = re.fullmatch(pattern, path)
    return found.groupdict() if found else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    server = MockToolServer(host=args.host, port=args.port).start()
    print(f"Mock tools on {server.base_url}: {', '.join(server.tools)}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()

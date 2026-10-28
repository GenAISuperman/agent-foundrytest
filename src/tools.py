"""Function tools for the tools bound in agent.yaml, built from their APIM OpenAPI exports.

For each bound tool the catalog fetches ``<apim_base_url>/openapi.json`` (``apim_base_url`` is the tool's full gateway route,
for example ``https://<apim>/tools/<id>``, as the portal's bind PR writes it) and
builds one function tool per operation in it. A call fetches the agent's own token for
the tool's scope and calls the operation on APIM, so APIM's per-operation role check
sees this agent. Only tools declared in agent.yaml are ever reachable: nothing here
takes a URL or tool id from the model or from the OpenAPI document.

Failures are returned to the model as text instead of raised, so a refused call (403)
is explained to the user and the request still completes.

A tool whose export cannot be fetched never slows requests down: startup waits a
bounded time for the exports, and later retries run in the background, spaced by an
exponential cooldown, while requests keep using the tools that are loaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, unquote

import httpx
from agent_framework import FunctionTool

from src.config import ToolBinding
from src.tokens import TokenSource

log = logging.getLogger(__name__)

EXPORT_PATH = "/openapi.json"
METHODS = ("get", "post", "put", "patch", "delete")
BODY_METHODS = ("post", "put", "patch")
MAX_RESULT_CHARS = 20_000
TIMEOUT_SECONDS = 30  # a tool call
EXPORT_TIMEOUT_SECONDS = 10  # fetching an export
STARTUP_BUDGET_SECONDS = 15  # all exports together, at startup
RETRY_COOLDOWN_SECONDS = 5  # after the first failure; doubles per failure up to the cap
MAX_RETRY_COOLDOWN_SECONDS = 300
MAX_NAME_LENGTH = 64

_SCALAR_TYPES = ("string", "integer", "number", "boolean")
_PARAM_TYPES = _SCALAR_TYPES + ("array",)
# OpenAPI: header parameters named like these are ignored (the transport owns them).
_IGNORED_HEADERS = ("accept", "content-type", "authorization")
_PLACEHOLDER = re.compile(r"\{([^}]+)\}")


# --- parsing an OpenAPI export ------------------------------------------------------


@dataclass(frozen=True)
class _Param:
    arg: str  # the argument name offered to the model
    name: str  # the name on the wire
    where: str  # "path" or "query"
    schema: dict
    required: bool


@dataclass(frozen=True)
class _Operation:
    method: str
    path: str
    operation_id: str
    summary: str
    params: tuple[_Param, ...]
    body_arg: str | None
    unsendable: tuple[str, ...]  # required header/cookie parameters, e.g. "header X-Tenant"


def _deref(document: dict, node: Any) -> dict | None:
    """Follow local ``#/...`` references. Anything else (remote refs, loops, junk) is None:
    nothing outside the exported document is ever fetched."""
    for _ in range(10):
        if not isinstance(node, dict):
            return None
        ref = node.get("$ref")
        if ref is None:
            return node
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        target: Any = document
        for part in ref[2:].split("/"):
            part = unquote(part).replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                target = target[int(part)]
            else:
                return None
        node = target
    return None


def _parameters(document: dict, *sources: Any) -> list[dict]:
    """Parameters of one operation: path-item level first, operation level overriding
    (same name and location), ``$ref`` entries resolved."""
    merged: dict[tuple[str, str], dict] = {}
    for source in sources:
        for entry in source if isinstance(source, list) else []:
            param = _deref(document, entry)
            if param is None or not isinstance(param.get("name"), str) or not param.get("in"):
                log.warning("ignoring a parameter that cannot be read: %r", entry)
                continue
            merged[(param["in"], param["name"])] = param
    return list(merged.values())


def _schema_for(document: dict, param: dict) -> dict:
    declared = _deref(document, param.get("schema")) or param  # Swagger 2 puts type on the parameter
    kind = declared.get("type")
    schema: dict[str, Any] = {"type": kind if kind in _PARAM_TYPES else "string"}
    if schema["type"] == "array":
        items = _deref(document, declared.get("items")) or {}
        schema["items"] = {"type": items.get("type") if items.get("type") in _SCALAR_TYPES else "string"}
    enum = declared.get("enum")
    if isinstance(enum, list) and enum and schema["type"] in _SCALAR_TYPES:
        schema["enum"] = enum
    schema["description"] = param.get("description") or f"{param['in']} parameter"
    return schema


def _parse_operation(
    document: dict, path: str, item: dict, method: str, operation: dict
) -> _Operation:
    params: list[_Param] = []
    unsendable: list[str] = []
    takes_body = method in BODY_METHODS or "requestBody" in operation
    taken = {"body"} if takes_body else set()
    declared = _parameters(document, item.get("parameters"), operation.get("parameters"))

    def add(where: str, name: str, schema: dict, required: bool) -> None:
        arg = name
        if arg in taken:  # same name in another location, or "body": keep both reachable
            arg = f"{where}_{name}"
            while arg in taken:
                arg += "_"
        taken.add(arg)
        params.append(_Param(arg, name, where, schema, required))

    for param in declared:
        if param["in"] == "path":
            add("path", param["name"], _schema_for(document, param), True)
    for placeholder in _PLACEHOLDER.findall(path):  # a placeholder the export forgot to declare
        if not any(p.where == "path" and p.name == placeholder for p in params):
            add("path", placeholder, {"type": "string", "description": "path parameter"}, True)
    for param in declared:
        if param["in"] == "query":
            add("query", param["name"], _schema_for(document, param), bool(param.get("required")))
        elif param["in"] in ("header", "cookie") and param.get("required"):
            if param["in"] == "header" and param["name"].lower() in _IGNORED_HEADERS:
                continue
            unsendable.append(f"{param['in']} {param['name']}")

    return _Operation(
        method=method,
        path=path,
        operation_id=operation.get("operationId") or f"{method}{path}",
        summary=operation.get("summary") or "",
        params=tuple(params),
        body_arg="body" if takes_body else None,
        unsendable=tuple(unsendable),
    )


def _parse_export(document: Any) -> list[_Operation]:
    if not isinstance(document, dict):
        raise RuntimeError("OpenAPI export is not a JSON object")
    operations = []
    for path, item in (document.get("paths") or {}).items():
        item = item if isinstance(item, dict) else {}
        for method in METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                operations.append(_parse_operation(document, path, item, method, operation))
    return operations


# --- function names -----------------------------------------------------------------


def _clean_name(tool_id: str, operation_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{tool_id}_{operation_id}")[:MAX_NAME_LENGTH]


def _assign_names(entries: list[tuple[ToolBinding, _Operation]]) -> list[str]:
    """One function name per operation. Names that collide (duplicate operation ids,
    characters replaced by ``_``, truncation) all get a hash suffix derived from the
    tool id, method, path and operation id, so the result does not depend on the order
    operations appear in an export."""
    base = [_clean_name(b.id, op.operation_id) for b, op in entries]
    counts = {name: base.count(name) for name in base}
    names = []
    for (binding, op), name in zip(entries, base):
        if counts[name] > 1:
            digest = hashlib.sha1(
                f"{binding.id}\0{op.method}\0{op.path}\0{op.operation_id}".encode()
            ).hexdigest()[:8]
            name = f"{name[: MAX_NAME_LENGTH - 9]}_{digest}"
        names.append(name)
    return names


# --- the catalog --------------------------------------------------------------------


class ToolCatalog:
    def __init__(
        self,
        bindings: tuple[ToolBinding, ...],
        token_source: TokenSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        startup_budget_seconds: float = STARTUP_BUDGET_SECONDS,
        retry_cooldown_seconds: float = RETRY_COOLDOWN_SECONDS,
        max_retry_cooldown_seconds: float = MAX_RETRY_COOLDOWN_SECONDS,
    ):
        self._bindings = bindings
        self._token_source = token_source
        self._clock = clock
        self._startup_budget = startup_budget_seconds
        self._cooldown = retry_cooldown_seconds
        self._max_cooldown = max_retry_cooldown_seconds
        self._loaded: dict[str, list[_Operation]] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._attempting: set[str] = set()
        self._background: set[asyncio.Task] = set()
        self._tools: list[FunctionTool] | None = None

    async def ensure_loaded(self) -> list[FunctionTool]:
        """Fetch the export of every bound tool that is not loaded and not cooling down.

        Waits at most the startup budget for all of them together (they run
        concurrently); an attempt still running then counts as a failure. A failed
        tool is logged and retried later (see ``available_tools``), so one bad tool
        neither stops the agent from starting nor stays missing once the cause is fixed.
        """
        tasks = [
            asyncio.ensure_future(self._attempt(b))
            for b in self._due()
        ]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self._startup_budget)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return self._current_tools()

    def available_tools(self) -> list[FunctionTool]:
        """The function tools loaded so far, without waiting for anything.

        Tools whose cooldown has passed are retried in a background task; they show up
        in a later call once they load.
        """
        for binding in self._due():
            task = asyncio.get_running_loop().create_task(self._attempt(binding))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return self._current_tools()

    async def close(self) -> None:
        """Stop background retries (the event loop is about to end)."""
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    def _due(self) -> list[ToolBinding]:
        now = self._clock()
        return [
            b
            for b in self._bindings
            if b.id not in self._loaded
            and b.id not in self._attempting
            and self._retry_at.get(b.id, 0) <= now
        ]

    async def _attempt(self, binding: ToolBinding) -> None:
        self._attempting.add(binding.id)
        try:
            self._loaded[binding.id] = await self._load(binding)
            self._failures.pop(binding.id, None)
            self._tools = None
        except asyncio.CancelledError:
            self._record_failure(binding, "timed out")
            raise
        except Exception as exc:  # noqa: BLE001 - never take the agent down
            self._record_failure(binding, exc)
        finally:
            self._attempting.discard(binding.id)

    def _record_failure(self, binding: ToolBinding, reason: object) -> None:
        count = self._failures.get(binding.id, 0) + 1
        self._failures[binding.id] = count
        wait = min(self._cooldown * 2 ** (count - 1), self._max_cooldown)
        self._retry_at[binding.id] = self._clock() + wait
        log.warning("tool %s unavailable: %s (retrying in %ss)", binding.id, reason, wait)

    def _current_tools(self) -> list[FunctionTool]:
        if self._tools is None:
            entries = [
                (b, op) for b in self._bindings for op in self._loaded.get(b.id, [])
            ]
            self._tools = [
                self._function_tool(binding, op, name)
                for (binding, op), name in zip(entries, _assign_names(entries))
            ]
        return list(self._tools)

    async def _load(self, binding: ToolBinding) -> list[_Operation]:
        url = f"{_base(binding)}{EXPORT_PATH}"
        status, text = await self._request(binding, "GET", url, timeout=EXPORT_TIMEOUT_SECONDS)
        if status != 200:
            raise RuntimeError(f"OpenAPI export returned HTTP {status}")
        return _parse_export(httpx.Response(200, text=text).json())

    def _function_tool(self, binding: ToolBinding, op: _Operation, name: str) -> FunctionTool:
        properties: dict[str, Any] = {p.arg: p.schema for p in op.params}
        required = [p.arg for p in op.params if p.required]
        if op.body_arg:
            properties[op.body_arg] = {"type": "object", "description": "JSON request body"}
        schema = {"type": "object", "properties": properties, "required": required}
        description = f"{op.summary} ({op.method.upper()} {op.path} on tool {binding.id})".strip()
        if op.unsendable:
            description += (
                f" Not callable: it requires {', '.join(op.unsendable)}, which this agent cannot send."
            )

        async def call(**arguments: Any) -> str:
            return await self._call(binding, op, arguments)

        return FunctionTool(name=name, description=description, func=call, input_model=schema)

    async def _call(self, binding: ToolBinding, op: _Operation, arguments: dict[str, Any]) -> str:
        if op.unsendable:
            return (
                f"Tool call not made: {op.method.upper()} {op.path} on tool '{binding.id}' requires "
                f"{', '.join(op.unsendable)}, which this agent cannot send. Tell the user it is unavailable."
            )
        segments: dict[str, str] = {}
        query: dict[str, Any] = {}
        for param in op.params:
            if param.arg not in arguments or arguments[param.arg] is None:
                if param.where == "path":
                    return f"Tool call not made: missing argument '{param.arg}'."
                continue
            value = _wire(arguments[param.arg])
            if param.where == "query":
                query[param.name] = value
                continue
            text = ",".join(value) if isinstance(value, list) else value
            if text.strip() in ("", ".", ".."):
                return (
                    f"Tool call not made: '{param.arg}' must be a real value, "
                    "not empty, '.' or '..'."
                )
            segments[param.name] = quote(text, safe="")
        route = _PLACEHOLDER.sub(lambda m: segments[m.group(1)], op.path)
        body = arguments.get(op.body_arg) if op.body_arg else None
        status, text = await self._request(
            binding, op.method.upper(), f"{_base(binding)}{route}", query or None, body
        )
        return _describe(binding, op.method, op.path, status, text)

    async def _request(
        self,
        binding: ToolBinding,
        method: str,
        url: str,
        query: dict | None = None,
        body: Any = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> tuple[int, str]:
        try:
            token = await asyncio.to_thread(self._token_source, binding.scope)
        except Exception as exc:  # noqa: BLE001
            log.warning("no token for %s: %s", binding.scope, exc)
            return 0, "token"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    url,
                    params=query,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            log.warning("tool %s unreachable: %s", binding.id, exc)
            return 0, "unreachable"
        return response.status_code, response.text


def _wire(value: Any) -> Any:
    """An argument as it goes on the wire: JSON-style booleans, lists element-wise."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    return str(value)


def _base(binding: ToolBinding) -> str:
    return binding.apim_base_url.rstrip('/')


def _describe(binding: ToolBinding, method: str, path: str, status: int, text: str) -> str:
    where = f"{method.upper()} {path} on tool '{binding.id}'"
    if status == 0:
        reason = (
            "the agent could not get a token for it"
            if text == "token"
            else "the tool could not be reached"
        )
        return f"The call to {where} was not made: {reason}. Tell the user it is unavailable."
    if status in (401, 403):
        return (
            f"The call to {where} was refused (HTTP {status}): this agent is not "
            "permitted to do that. Tell the user you do not have access to this action; "
            "do not retry it."
        )
    if status >= 400:
        return f"The call to {where} failed with HTTP {status}. Tell the user it did not work."
    return text[:MAX_RESULT_CHARS]

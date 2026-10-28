"""ToolCatalog against odd-but-valid OpenAPI exports, in process (no model involved)."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.tools import ToolCatalog
from tests.openapi_stub import OpenApiStub, document

ID_PARAM = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


@pytest.fixture
def stub_for():
    started: list[OpenApiStub] = []

    def make(paths, components=None, **kwargs) -> OpenApiStub:
        stub = OpenApiStub(document(paths, components), **kwargs).start()
        started.append(stub)
        return stub

    yield make
    for stub in started:
        stub.stop()


def load(stub: OpenApiStub, **kwargs):
    catalog = ToolCatalog((stub.binding(),), lambda scope: "tok", **kwargs)
    tools = asyncio.run(catalog.ensure_loaded())
    return catalog, {t.name: t for t in tools}


def call(tool, **arguments) -> str:
    async def go():
        return await tool.invoke(arguments=arguments, skip_parsing=True)

    return asyncio.run(go())


# --- parameters: path-item level and $ref ------------------------------------------


def test_path_item_level_parameters_are_used(stub_for):
    stub = stub_for(
        {
            "/items/{id}": {
                "parameters": [ID_PARAM, {"name": "q", "in": "query", "schema": {"type": "string"}}],
                "get": {"operationId": "get-item"},
            }
        }
    )
    _, tools = load(stub)

    assert set(tools["demo_get-item"].parameters()["properties"]) == {"id", "q"}
    assert tools["demo_get-item"].parameters()["required"] == ["id"]
    call(tools["demo_get-item"], id="7", q="x")
    (seen,) = stub.calls
    assert seen.path == "/tools/demo/items/7"
    assert seen.query == {"q": ["x"]}


def test_operation_parameters_override_path_item_parameters(stub_for):
    stub = stub_for(
        {
            "/items": {
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "string"}}],
                "get": {
                    "operationId": "list",
                    "parameters": [
                        {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}}
                    ],
                },
            }
        }
    )
    _, tools = load(stub)

    schema = tools["demo_list"].parameters()
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["required"] == ["limit"]


def test_local_ref_parameters_are_resolved(stub_for):
    stub = stub_for(
        {
            "/items/{id}": {
                "parameters": [{"$ref": "#/components/parameters/ItemId"}],
                "get": {
                    "operationId": "get-item",
                    "parameters": [{"$ref": "#/components/parameters/Verbose"}],
                },
            }
        },
        components={
            "parameters": {
                "ItemId": ID_PARAM,
                "Verbose": {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
            }
        },
    )
    _, tools = load(stub)

    assert set(tools["demo_get-item"].parameters()["properties"]) == {"id", "verbose"}
    call(tools["demo_get-item"], id="7", verbose=True)
    (seen,) = stub.calls
    assert seen.path == "/tools/demo/items/7"
    assert seen.query == {"verbose": ["true"]}


def test_a_path_placeholder_without_a_declared_parameter_is_still_a_required_argument(stub_for):
    stub = stub_for({"/items/{id}": {"get": {"operationId": "get-item"}}})
    _, tools = load(stub)

    assert tools["demo_get-item"].parameters()["required"] == ["id"]


# --- path argument values ----------------------------------------------------------


@pytest.mark.parametrize("value", [".", "..", "", " .. "])
def test_dot_segments_and_empty_values_are_refused_for_path_arguments(stub_for, value):
    stub = stub_for({"/items/{id}": {"get": {"operationId": "get-item", "parameters": [ID_PARAM]}}})
    _, tools = load(stub)

    reply = call(tools["demo_get-item"], id=value)

    assert "not made" in reply and "'id'" in reply
    assert stub.calls == []


def test_ordinary_path_values_are_encoded_and_sent(stub_for):
    stub = stub_for({"/items/{id}": {"get": {"operationId": "get-item", "parameters": [ID_PARAM]}}})
    _, tools = load(stub)

    call(tools["demo_get-item"], id="a/b..c")

    assert stub.calls[0].path == "/tools/demo/items/a%2Fb..c"


# --- schema fidelity ---------------------------------------------------------------


def test_declared_parameter_types_reach_the_function_schema(stub_for):
    stub = stub_for(
        {
            "/items": {
                "get": {
                    "operationId": "list",
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                        {"name": "ratio", "in": "query", "schema": {"type": "number"}},
                        {"name": "all", "in": "query", "schema": {"type": "boolean"}},
                        {
                            "name": "tag",
                            "in": "query",
                            "schema": {"type": "array", "items": {"type": "integer"}},
                        },
                        {"name": "kind", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}},
                        {"name": "plain", "in": "query"},
                    ],
                }
            }
        }
    )
    _, tools = load(stub)

    props = tools["demo_list"].parameters()["properties"]
    assert props["limit"]["type"] == "integer"
    assert props["ratio"]["type"] == "number"
    assert props["all"]["type"] == "boolean"
    assert props["tag"]["type"] == "array" and props["tag"]["items"] == {"type": "integer"}
    assert props["kind"]["enum"] == ["a", "b"]
    assert props["plain"]["type"] == "string"

    call(tools["demo_list"], limit=5, all=False, tag=[1, 2])
    assert stub.calls[0].query == {"limit": ["5"], "all": ["false"], "tag": ["1", "2"]}


def test_a_parameter_named_body_does_not_collide_with_the_request_body(stub_for):
    stub = stub_for(
        {
            "/items": {
                "post": {
                    "operationId": "create",
                    "parameters": [{"name": "body", "in": "query", "schema": {"type": "string"}}],
                }
            }
        }
    )
    _, tools = load(stub)

    props = tools["demo_create"].parameters()["properties"]
    assert len(props) == 2 and props["body"]["type"] == "object"
    (other,) = [name for name in props if name != "body"]

    call(tools["demo_create"], body={"x": 1}, **{other: "q"})

    (seen,) = stub.calls
    assert seen.body == {"x": 1}
    assert seen.query == {"body": ["q"]}


def test_a_required_header_the_agent_cannot_send_is_reported_to_the_model(stub_for):
    stub = stub_for(
        {
            "/items": {
                "get": {
                    "operationId": "list",
                    "parameters": [
                        {"name": "X-Tenant", "in": "header", "required": True, "schema": {"type": "string"}},
                        {"name": "Accept", "in": "header", "required": True, "schema": {"type": "string"}},
                    ],
                }
            }
        }
    )
    _, tools = load(stub)

    reply = call(tools["demo_list"])

    assert "not made" in reply and "X-Tenant" in reply and "header" in reply
    assert "Accept" not in reply  # OpenAPI says Accept, Content-Type, Authorization are ignored
    assert stub.calls == []
    assert "X-Tenant" in tools["demo_list"].description


def test_an_optional_header_or_cookie_does_not_stop_the_call(stub_for):
    stub = stub_for(
        {
            "/items": {
                "get": {
                    "operationId": "list",
                    "parameters": [
                        {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                        {"name": "sid", "in": "cookie", "schema": {"type": "string"}},
                    ],
                }
            }
        }
    )
    _, tools = load(stub)

    assert "ok" in call(tools["demo_list"])
    assert len(stub.calls) == 1


# --- function names ----------------------------------------------------------------


def _names(paths) -> list[str]:
    stub = OpenApiStub(document(paths)).start()
    try:
        _, tools = load(stub)
        return sorted(tools)
    finally:
        stub.stop()


def test_colliding_function_names_are_disambiguated_deterministically():
    forward = {
        "/a": {"get": {"operationId": "get/x"}},
        "/b": {"get": {"operationId": "get_x"}},
        "/c": {"get": {"operationId": "other"}},
    }
    backward = dict(reversed(list(forward.items())))

    first, second = _names(forward), _names(backward)

    assert len(first) == 3 and len(set(first)) == 3
    assert first == second  # independent of the order operations appear in the document
    assert "demo_other" in first  # names that did not collide are left alone


def test_names_that_collide_after_truncation_are_disambiguated():
    long_a = "x" * 80 + "a"
    long_b = "x" * 80 + "b"
    names = _names(
        {"/a": {"get": {"operationId": long_a}}, "/b": {"get": {"operationId": long_b}}}
    )

    assert len(names) == 2 and all(len(n) <= 64 for n in names)


def test_duplicate_operation_ids_in_one_export_are_disambiguated():
    names = _names(
        {"/a": {"get": {"operationId": "same"}}, "/b": {"get": {"operationId": "same"}}}
    )

    assert len(names) == 2


# --- failing tools: cooldown, no lock, bounded startup ------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_failing_tool_is_not_retried_until_its_cooldown_has_passed(stub_for):
    stub = stub_for({"/x": {"get": {"operationId": "x"}}}, export_status=403)
    clock = Clock()
    catalog = ToolCatalog(
        (stub.binding(),), lambda s: "t", clock=clock, retry_cooldown_seconds=10
    )

    async def scenario():
        await catalog.ensure_loaded()
        assert stub.export_requests == 1
        for _ in range(5):  # requests arriving during the cooldown
            catalog.available_tools()
            await asyncio.sleep(0.05)
        assert stub.export_requests == 1
        clock.now += 11
        catalog.available_tools()
        await asyncio.sleep(0.5)
        assert stub.export_requests == 2
        clock.now += 11  # the second failure doubles the cooldown: 11s is not enough
        catalog.available_tools()
        await asyncio.sleep(0.3)
        assert stub.export_requests == 2

    asyncio.run(scenario())


def test_a_tool_that_recovers_is_picked_up_after_the_cooldown(stub_for):
    stub = stub_for({"/x": {"get": {"operationId": "x"}}}, export_status=403)
    clock = Clock()
    catalog = ToolCatalog(
        (stub.binding(),), lambda s: "t", clock=clock, retry_cooldown_seconds=10
    )

    async def scenario():
        assert await catalog.ensure_loaded() == []
        stub.export_status = 200
        clock.now += 11
        assert catalog.available_tools() == []  # the retry runs in the background...
        for _ in range(50):
            await asyncio.sleep(0.1)
            if catalog.available_tools():
                break
        return [t.name for t in catalog.available_tools()]

    assert asyncio.run(scenario()) == ["demo_x"]


def test_a_slow_failing_tool_does_not_delay_requests(stub_for):
    stub = stub_for({"/x": {"get": {"operationId": "x"}}}, export_status=500, export_delay=2)
    clock = Clock()
    catalog = ToolCatalog(
        (stub.binding(),), lambda s: "t", clock=clock, retry_cooldown_seconds=1, startup_budget_seconds=0.2
    )

    async def scenario():
        await catalog.ensure_loaded()
        clock.now += 5
        started = time.monotonic()
        results = [catalog.available_tools() for _ in range(20)]
        elapsed = time.monotonic() - started
        assert elapsed < 0.5 and all(r == [] for r in results)
        # nothing is left running when the loop ends
        await catalog.close()

    asyncio.run(scenario())


def test_startup_waits_a_bounded_time_for_a_hanging_tool(stub_for):
    stub = stub_for({"/x": {"get": {"operationId": "x"}}}, export_delay=3)
    catalog = ToolCatalog((stub.binding(),), lambda s: "t", startup_budget_seconds=0.5)

    started = time.monotonic()
    tools = asyncio.run(catalog.ensure_loaded())

    assert tools == []
    assert time.monotonic() - started < 2.5


def test_one_hanging_tool_does_not_hold_back_a_healthy_one(stub_for):
    slow = stub_for({"/x": {"get": {"operationId": "x"}}}, export_delay=3)
    fast = stub_for({"/y": {"get": {"operationId": "y"}}})
    catalog = ToolCatalog(
        (slow.binding("slow"), fast.binding("fast")),
        lambda s: "t",
        startup_budget_seconds=1,
    )

    names = [t.name for t in asyncio.run(catalog.ensure_loaded())]

    assert names == ["fast_y"]

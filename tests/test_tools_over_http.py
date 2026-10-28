"""Seam 1 for tools: the agent over HTTP, with a stub model and a stub APIM tool server."""

import time

import httpx


def invoke(agent, text):
    response = httpx.post(
        f"{agent.url}/invocations",
        json={"messages": [{"role": "user", "content": text}]},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return response.json()["choices"][0]["message"]["content"]


def shop_tool(server, roles=("read",)):
    """The agent.yaml entry a bind PR writes."""
    return {
        "id": "mock-shop-api",
        "roles": list(roles),
        "apim_base_url": f"{server.base_url}/tools/mock-shop-api",
        "scope": "api://mock-shop-app-id/.default",
    }


def test_bound_read_tool_is_called_with_the_agents_token_and_its_result_used(
    agent_with_tools, tool_server
):
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=["read"])

    reply = invoke(agent, "CALL mock-shop-api_get-products {}")

    assert "Rain jacket" in reply
    calls = tool_server.requests_to("/tools/mock-shop-api/products")
    assert [c.method for c in calls] == ["GET"]
    assert calls[0].authorization == (
        "Bearer stub-token|scope=api://mock-shop-app-id/.default|roles=read"
    )


def test_call_arguments_reach_the_tool_route(agent_with_tools, tool_server):
    agent = agent_with_tools(
        [shop_tool(tool_server, roles=("read", "write"))], token_roles=["read", "write"]
    )

    product = invoke(agent, 'CALL mock-shop-api_get-products-id {"id": "2"}')
    added = invoke(
        agent,
        'CALL mock-shop-api_post-basket-items {"body": {"product_id": "2", "quantity": 1}}',
    )

    assert "Denim jacket" in product
    assert "quantity" in added
    posted = tool_server.requests_to("/tools/mock-shop-api/basket/items")
    assert posted[0].body == {"product_id": "2", "quantity": 1}


def test_path_arguments_cannot_escape_the_tools_route(agent_with_tools, tool_server):
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=["read"])

    invoke(agent, 'CALL mock-shop-api_get-products-id {"id": "../../other-api/secrets"}')

    assert tool_server.requests_to("/tools/other-api") == []


def test_operations_come_from_the_tools_openapi_export(agent_with_tools, tool_server):
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=["read"])

    reply = invoke(agent, "LIST TOOLS")

    assert reply == (
        "tools: mock-shop-api_get-products, mock-shop-api_get-products-id, "
        "mock-shop-api_post-basket-items"
    )
    export_calls = tool_server.requests_to("/tools/mock-shop-api/openapi.json")
    assert export_calls and export_calls[0].authorization.startswith("Bearer stub-token|")


def test_refused_call_is_explained_and_the_request_still_returns(
    agent_with_tools, tool_server
):
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=["read"])

    reply = invoke(
        agent, 'CALL mock-shop-api_post-basket-items {"body": {"product_id": "2"}}'
    )

    assert "refused (HTTP 403)" in reply
    assert "do not have access" in reply
    attempts = tool_server.requests_to("/tools/mock-shop-api/basket/items")
    assert len(attempts) == 1  # the agent asked once; APIM, not the agent, said no


def test_tool_absent_from_agent_yaml_is_never_called(agent_with_tools, tool_server):
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=["read"])

    reply = invoke(agent, "CALL other-api_get-secrets {}")

    assert "launch-codes" not in reply
    assert tool_server.requests_to("/tools/other-api") == []
    assert "other-api" not in invoke(agent, "LIST TOOLS")


def test_agent_with_no_tools_answers_plain_questions(agent_with_tools, tool_server):
    agent = agent_with_tools([])

    assert invoke(agent, "Hello").startswith("echo: Hello")
    assert invoke(agent, "LIST TOOLS") == "tools: "
    assert tool_server.requests == []


def test_unreachable_tool_does_not_stop_the_agent(agent_with_tools, tool_server):
    broken = {**shop_tool(tool_server), "apim_base_url": "http://127.0.0.1:9"}
    agent = agent_with_tools([broken], token_roles=["read"])

    assert invoke(agent, "Hello").startswith("echo: Hello")


def test_failing_tool_is_retried_in_the_background_not_on_every_request(
    agent_with_tools, tool_server
):
    # No role at first: APIM refuses the export, so the tool is not offered...
    agent = agent_with_tools([shop_tool(tool_server)], token_roles=[])
    exports = lambda: len(tool_server.requests_to("/tools/mock-shop-api/openapi.json"))
    assert invoke(agent, "LIST TOOLS") == "tools: "
    # ...and the agent asks again after a cooldown (0.3s, doubling, in this test), instead
    # of staying without the tool forever. A request never waits for the retry.
    before = exports()
    deadline = time.time() + 10
    while exports() == before and time.time() < deadline:
        started = time.time()
        assert invoke(agent, "LIST TOOLS") == "tools: "
        assert time.time() - started < 2
        time.sleep(0.2)
    assert exports() > before

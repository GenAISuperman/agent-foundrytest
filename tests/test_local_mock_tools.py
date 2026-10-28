"""The local-run path: the mock tool server plus AGENT_LOCAL_MOCK_TOOLS=1, no Azure needed."""

import asyncio

from dev.mock_tool_server import MockToolServer
from src.config import ToolBinding
from src.tokens import token_source_from_env
from src.tools import ToolCatalog


def test_catalog_works_against_the_mock_server_without_azure(monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_MOCK_TOOLS", "1")
    server = MockToolServer().start()  # lenient: any bearer token is fine
    try:
        binding = ToolBinding(
            "mock-shop-api", ("read",), f"{server.base_url}/tools/mock-shop-api", "api://local/.default"
        )

        async def run():
            catalog = ToolCatalog((binding,), token_source_from_env())
            tools = {t.name: t for t in await catalog.ensure_loaded()}
            result = await tools["mock-shop-api_get-products"].invoke(arguments={})
            return sorted(tools), result[0].text

        names, result = asyncio.run(run())
    finally:
        server.stop()

    assert "mock-shop-api_get-products" in names
    assert "Rain jacket" in result
    assert {r.authorization for r in server.requests} == {"Bearer mock-token"}

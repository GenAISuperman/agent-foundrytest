"""Run the real agent web layer with the stub model, for tests.

Usage: python -m tests.stub_server   (reads AGENT_YAML_PATH, PORT, STUB_TOKEN_ROLES, STUB_TOOL_COOLDOWN)

The token source is a stub: its tokens name the scope asked for and the roles from
STUB_TOKEN_ROLES, which the stub tool server uses to play APIM's role check.
"""

import asyncio
import os

from src.app import build_host
from src.config import load_config
from src.tools import ToolCatalog
from tests.stub_model import StubChatClient


def stub_token(scope: str) -> str:
    return f"stub-token|scope={scope}|roles={os.environ.get('STUB_TOKEN_ROLES', '')}"


if __name__ == "__main__":
    config = load_config()
    catalog = ToolCatalog(
        config.tools,
        stub_token,
        retry_cooldown_seconds=float(os.environ.get("STUB_TOOL_COOLDOWN", "0.3")),
    )
    asyncio.run(catalog.ensure_loaded())
    build_host(config, StubChatClient(), catalog).run()

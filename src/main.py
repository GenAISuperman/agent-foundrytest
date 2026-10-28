"""Container entry point: validate agent.yaml, then serve the agent."""

from __future__ import annotations

import asyncio
import sys

from src.config import ConfigError, load_config


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(str(exc))

    from agent_framework.foundry import FoundryChatClient
    from azure.identity import DefaultAzureCredential

    from src.app import build_host
    from src.tokens import token_source_from_env
    from src.tools import ToolCatalog

    chat_client = FoundryChatClient(
        project_endpoint=config.project_endpoint,
        model=config.model_deployment,
        credential=DefaultAzureCredential(),
    )
    catalog = ToolCatalog(config.tools, token_source_from_env())
    # Fetch each bound tool's OpenAPI export now; a tool that fails here is retried per request.
    asyncio.run(catalog.ensure_loaded())
    build_host(config, chat_client, catalog).run()


if __name__ == "__main__":
    main()

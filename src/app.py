"""Web layer: Foundry's Invocations protocol host around an Agent Framework agent."""

from __future__ import annotations

from agent_framework import Agent
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src import chat_completions
from src.config import AgentConfig
from src.tools import ToolCatalog


def build_host(
    config: AgentConfig, chat_client, catalog: ToolCatalog
) -> InvocationAgentServerHost:
    host = InvocationAgentServerHost()

    @host.invoke_handler
    async def handle(request: Request) -> Response:
        try:
            messages = chat_completions.to_messages(await request.json())
        except (chat_completions.InvalidRequest, ValueError) as exc:
            message = (
                str(exc)
                if isinstance(exc, chat_completions.InvalidRequest)
                else "request body is not valid JSON"
            )
            return JSONResponse(chat_completions.to_error(message), status_code=400)
        instructions = config.prompt_path.read_text(encoding="utf-8").strip()
        agent = Agent(
            client=chat_client,
            instructions=instructions,
            tools=catalog.available_tools(),
        )
        result = await agent.run(messages)
        usage = result.usage_details
        return JSONResponse(
            chat_completions.to_response(config.agent_id, result.text, usage)
        )

    return host

"""A stub chat model: deterministic replies that expose what the agent sent.

Scripted through the last user message, so tests drive it over HTTP:

- ``CALL <function_name> <json arguments>``: the model asks to call that function.
  Any name is accepted, bound or not; the agent decides whether it can run.
- ``LIST TOOLS``: replies with the names of the functions the agent offered.
- anything else: ``echo: <last user text> | system: <instructions>``.

After a function result comes back the model replies ``tool result: <result text>``.
"""

from __future__ import annotations

import json

from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)


def _tool_names(options) -> list[str]:
    return [getattr(tool, "name", str(tool)) for tool in options.get("tools") or []]


class StubChatClient(FunctionInvocationLayer, BaseChatClient):
    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        if stream:
            raise NotImplementedError("stub does not stream")

        async def _reply() -> ChatResponse:
            messages_list = list(messages)
            last = messages_list[-1] if messages_list else None
            if last is not None and last.role == "tool":
                results = [
                    str(c.result) for c in last.contents if c.type == "function_result"
                ]
                return _text(f"tool result: {' '.join(results)}")

            last_user = next(
                (m.text for m in reversed(messages_list) if m.role == "user"), ""
            )
            if last_user.startswith("CALL "):
                _, name, *rest = last_user.split(" ", 2)
                arguments = json.loads(rest[0]) if rest else {}
                call = Content.from_function_call(
                    call_id="call-1", name=name, arguments=arguments
                )
                return ChatResponse(
                    messages=[Message(role="assistant", contents=[call])],
                    response_id="stub-response",
                    usage_details={"input_token_count": 5, "output_token_count": 7},
                )
            if last_user == "LIST TOOLS":
                return _text("tools: " + ", ".join(sorted(_tool_names(options))))

            system_text = " ".join(
                m.text for m in messages_list if m.role == "system" and m.text
            )
            instructions = options.get("instructions") or ""
            seen_system = " ".join(part for part in (instructions, system_text) if part)
            return _text(f"echo: {last_user} | system: {seen_system}")

        return _reply()


def _text(text: str) -> ChatResponse:
    return ChatResponse(
        messages=[Message(role="assistant", contents=[text])],
        response_id="stub-response",
        usage_details={"input_token_count": 5, "output_token_count": 7},
    )

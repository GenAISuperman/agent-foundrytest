"""Protocol mapping layer: OpenAI-style chat-completions body <-> agent calls.

The Invocations protocol passes the request body through uninterpreted, so the
body shape is ours to define. We use the chat-completions shape so callers see
the same contract as on AKS. Keep every assumption about that shape in this
module; the web layer and the agent do not know about it.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from agent_framework import Message


ROLES = {"system", "user", "assistant"}


class InvalidRequest(Exception):
    """The request body is not a usable chat-completions request."""


def to_messages(body: Any) -> list[Message]:
    """Validate the request body and map its chat messages to Agent Framework messages."""
    if not isinstance(body, dict):
        raise InvalidRequest("request body must be a JSON object")
    if body.get("stream"):
        raise InvalidRequest("streaming is not supported; send stream=false")
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise InvalidRequest("messages must be a non-empty list")
    messages = []
    for index, item in enumerate(raw):
        if (
            not isinstance(item, dict)
            or item.get("role") not in ROLES
            or not isinstance(item.get("content"), str)
        ):
            raise InvalidRequest(
                f"messages[{index}] needs a role ({', '.join(sorted(ROLES))}) "
                "and string content"
            )
        messages.append(Message(role=item["role"], contents=[item["content"]]))
    return messages


def to_error(message: str) -> dict:
    """Map a rejected request to a chat-completions style error body."""
    return {"error": {"message": message, "type": "invalid_request_error"}}


def to_response(agent_id: str, text: str, usage: dict[str, int | None] | None) -> dict:
    """Map the agent's reply to a chat-completions response body."""
    usage = usage or {}
    prompt = usage.get("input_token_count") or 0
    completion = usage.get("output_token_count") or 0
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": agent_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }

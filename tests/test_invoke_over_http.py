import httpx


def invoke(agent, body, **kwargs):
    return httpx.post(f"{agent.url}/invocations", json=body, timeout=30, **kwargs)


def test_chat_completions_body_returns_chat_completions_reply(agent):
    response = invoke(
        agent,
        {"model": "shopping", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "shopping"
    assert isinstance(body["created"], int)
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].startswith("echo: Hello")
    assert body["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }


def test_editing_the_prompt_file_changes_behaviour_without_code_changes(agent):
    body = {"messages": [{"role": "user", "content": "Hi"}]}

    first = invoke(agent, body).json()["choices"][0]["message"]["content"]
    agent.prompt_file.write_text("You are a pirate.\n")
    second = invoke(agent, body).json()["choices"][0]["message"]["content"]

    assert "You are a helpful AI assistant." in first
    assert "You are a pirate." in second
    assert "helpful" not in second


def test_conversation_history_reaches_the_model(agent):
    response = invoke(
        agent,
        {
            "messages": [
                {"role": "user", "content": "My name is Alice"},
                {"role": "assistant", "content": "Hi Alice"},
                {"role": "user", "content": "What is my name?"},
            ]
        },
    )

    assert response.json()["choices"][0]["message"]["content"].startswith(
        "echo: What is my name?"
    )


def test_malformed_requests_get_a_chat_completions_style_400(agent):
    bad_bodies = [
        {},
        {"messages": []},
        {"messages": "hello"},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": "hi"}], "stream": True},
    ]
    for body in bad_bodies:
        response = invoke(agent, body)
        assert response.status_code == 400, body
        error = response.json()["error"]
        assert error["type"] == "invalid_request_error"
        assert error["message"]

    not_json = httpx.post(
        f"{agent.url}/invocations",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert not_json.status_code == 400

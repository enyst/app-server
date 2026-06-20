from __future__ import annotations


def _conversation_id(client, authed_headers) -> str:
    return client.post(
        "/api/v1/app-conversations",
        json={"initial_message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        headers=authed_headers,
    ).json()["app_conversation_id"]


def test_runtime_proxy_routes(client, authed_headers, fake_agent_server):
    conversation_id = _conversation_id(client, authed_headers)

    assert client.get(
        f"/api/conversations/{conversation_id}/events/count", headers=authed_headers
    ).json() == 3
    assert client.post(
        f"/api/conversations/{conversation_id}/ask_agent",
        json={"question": "status?"},
        headers=authed_headers,
    ).json() == {"response": "answer: status?"}
    assert client.post(f"/api/conversations/{conversation_id}/pause", headers=authed_headers).json() == {
        "success": True
    }
    assert client.post(f"/api/conversations/{conversation_id}/run", headers=authed_headers).json() == {
        "success": True
    }

    confirmation = client.post(
        f"/api/conversations/{conversation_id}/events/respond_to_confirmation",
        json={"confirmed": True},
        headers=authed_headers,
    )
    assert confirmation.status_code == 200
    assert fake_agent_server.state.received[-1]["path"] == "confirm"


def test_event_history_and_git_proxy_routes(client, authed_headers):
    conversation_id = _conversation_id(client, authed_headers)

    events = client.get(
        f"/api/v1/conversation/{conversation_id}/events/search", headers=authed_headers
    )
    assert events.status_code == 200
    assert events.json()["items"][0]["message"] == "hello"

    changes = client.get(
        "/api/v1/git/changes",
        params={"conversation_id": conversation_id, "path": "/workspace/project"},
        headers=authed_headers,
    )
    assert changes.status_code == 200
    assert changes.json()[0]["path"] == "/workspace/project/README.md"

    diff = client.get(
        "/api/v1/git/diff",
        params={"conversation_id": conversation_id, "path": "/workspace/project/README.md"},
        headers=authed_headers,
    )
    assert diff.status_code == 200
    assert "diff --git" in diff.json()["diff"]

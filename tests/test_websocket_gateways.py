from __future__ import annotations


def _conversation_id(client, authed_headers) -> str:
    return client.post(
        "/api/v1/app-conversations",
        json={"initial_message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        headers=authed_headers,
    ).json()["app_conversation_id"]


def test_event_websocket_gateway(client, authed_headers):
    conversation_id = _conversation_id(client, authed_headers)
    with client.websocket_connect(
        f"/ws/events/{conversation_id}?session_api_key=app-secret&resend_mode=since&after_timestamp=2026-01-01T00:00:00Z"
    ) as websocket:
        assert websocket.receive_json() == {"type": "runtime_event", "conversation_id": conversation_id}


def test_bash_websocket_gateway(client, authed_headers):
    conversation_id = _conversation_id(client, authed_headers)
    with client.websocket_connect(f"/ws/bash-events/{conversation_id}?session_api_key=app-secret") as websocket:
        assert websocket.receive_json() == {"type": "bash_event", "message": "ready"}

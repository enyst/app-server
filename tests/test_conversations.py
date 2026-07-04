from __future__ import annotations

from conftest import assert_uuid


def _start_payload() -> dict:
    return {
        "initial_message": {"role": "user", "content": [{"type": "text", "text": "hello"}], "run": True},
        "agent_settings": {"llm": {"model": "test-model", "api_key": "secret"}},
        "conversation_settings": {"max_iterations": 5},
        "secrets": {"TOKEN": "value"},
        "workspace": {"kind": "LocalWorkspace", "working_dir": "/workspace/project"},
    }


def test_start_conversation_creates_runtime_conversation(client, authed_headers, fake_agent_server):
    response = client.post("/api/v1/app-conversations", json=_start_payload(), headers=authed_headers)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "READY"
    conversation_id = task["app_conversation_id"]
    assert_uuid(conversation_id)

    runtime_request = fake_agent_server.state.conversations[conversation_id]
    assert runtime_request["agent_settings"]["llm"]["model"] == "test-model"
    assert runtime_request["conversation_settings"]["max_iterations"] == 5
    assert runtime_request["secrets"]["TOKEN"] == "value"

    batch = client.get(
        "/api/v1/app-conversations",
        params=[("ids", conversation_id)],
        headers=authed_headers,
    )
    assert batch.status_code == 200
    conversation = batch.json()[0]
    assert conversation["id"] == conversation_id
    assert conversation["conversation_url"].endswith(f"/api/conversations/{conversation_id}")
    assert conversation["websocket_url"] == f"/ws/events/{conversation_id}"
    assert conversation["session_api_key"] == "runtime-secret"

    search = client.get("/api/v1/app-conversations/search", headers=authed_headers)
    assert search.json()["items"][0]["id"] == conversation_id

    tasks = client.get(
        "/api/v1/app-conversations/start-tasks",
        params=[("ids", task["id"])],
        headers=authed_headers,
    )
    assert tasks.json()[0]["app_conversation_id"] == conversation_id


def test_send_message_uses_app_server_gateway(client, authed_headers, fake_agent_server):
    conversation_id = client.post(
        "/api/v1/app-conversations", json=_start_payload(), headers=authed_headers
    ).json()["app_conversation_id"]

    response = client.post(
        f"/api/v1/app-conversations/{conversation_id}/send-message",
        json={"role": "user", "content": [{"type": "text", "text": "follow up"}], "run": True},
        headers=authed_headers,
    )
    assert response.status_code == 200
    assert fake_agent_server.state.received[-1]["path"] == "events"
    assert fake_agent_server.state.received[-1]["body"]["content"][0]["text"] == "follow up"


def test_app_conversation_state_persists_across_app_restart(
    fake_agent_server, tmp_path, authed_headers
):
    from fastapi.testclient import TestClient

    from app_server.app import create_app
    from app_server.config import AppServerConfig

    config = AppServerConfig(
        session_api_keys=["app-secret"],
        state_dir=tmp_path,
        static_agent_server_url=fake_agent_server.base_url,
        static_agent_server_session_key="runtime-secret",
    )
    with TestClient(create_app(config)) as first_client:
        task = first_client.post(
            "/api/v1/app-conversations", json=_start_payload(), headers=authed_headers
        ).json()
        conversation_id = task["app_conversation_id"]
        sandbox_id = task["sandbox_id"]

    with TestClient(create_app(config)) as second_client:
        conversation = second_client.get(
            "/api/v1/app-conversations",
            params=[("ids", conversation_id)],
            headers=authed_headers,
        ).json()[0]
        assert conversation["id"] == conversation_id
        sandbox = second_client.get(
            "/api/v1/sandboxes",
            params=[("id", sandbox_id)],
            headers=authed_headers,
        ).json()[0]
        assert sandbox["id"] == sandbox_id
        task_after_restart = second_client.get(
            "/api/v1/app-conversations/start-tasks",
            params=[("ids", task["id"])],
            headers=authed_headers,
        ).json()[0]
        assert task_after_restart["app_conversation_id"] == conversation_id


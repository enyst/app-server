from __future__ import annotations

from conftest import TEST_LLM_API_KEY, TEST_MODEL

# ── Custom secrets ──────────────────────────────────────────────────


def test_create_list_and_delete_a_secret(client, authed_headers):
    created = client.post(
        "/api/v1/secrets",
        json={"name": "MY_TOKEN", "value": "s3cret", "description": "a token"},
        headers=authed_headers,
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/secrets", headers=authed_headers).json()
    assert listed["secrets"] == [{"name": "MY_TOKEN", "description": "a token"}]
    # Listing must never expose values.
    assert "s3cret" not in str(listed)

    assert client.delete("/api/v1/secrets/MY_TOKEN", headers=authed_headers).status_code == 200
    assert client.get("/api/v1/secrets", headers=authed_headers).json()["secrets"] == []


def test_secret_names_must_be_valid_env_vars(client, authed_headers):
    response = client.post(
        "/api/v1/secrets",
        json={"name": "not-a-valid-name", "value": "x"},
        headers=authed_headers,
    )
    assert response.status_code == 422


def test_provider_token_shows_up_as_set_without_leaking(client, authed_headers):
    stored = client.post(
        "/api/v1/secrets/provider-tokens/github",
        json={"token": "ghp_secret", "host": "github.com"},
        headers=authed_headers,
    )
    assert stored.status_code == 200, stored.text

    settings = client.get("/api/v1/settings", headers=authed_headers).json()
    assert settings["provider_tokens_set"] == {"github": "github.com"}
    assert "ghp_secret" not in str(settings)


# ── Start request construction ──────────────────────────────────────


def test_start_request_refuses_when_nothing_is_configured(unconfigured_client, authed_headers):
    response = unconfigured_client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hi"},
        headers=authed_headers,
    )
    assert response.status_code == 400
    assert "settings" in response.json()["detail"].lower()


def test_start_request_refuses_without_an_api_key(unconfigured_client, authed_headers):
    saved = unconfigured_client.post(
        "/api/v1/settings",
        json={"agent_settings_diff": {"llm": {"model": "gpt-4o"}}},
        headers=authed_headers,
    )
    assert saved.status_code == 200, saved.text

    response = unconfigured_client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hi"},
        headers=authed_headers,
    )
    assert response.status_code == 400
    assert "api key" in response.json()["detail"].lower()


def test_start_request_carries_persisted_llm_and_secrets(client, authed_headers, fake_agent_server):
    client.post(
        "/api/v1/secrets",
        json={"name": "MY_TOKEN", "value": "s3cret"},
        headers=authed_headers,
    )

    task = client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hello"},
        headers=authed_headers,
    ).json()

    request = fake_agent_server.state.conversations[task["app_conversation_id"]]
    assert request["agent"]["llm"]["model"] == TEST_MODEL
    assert request["agent"]["llm"]["api_key"] == TEST_LLM_API_KEY
    assert request["secrets"]["MY_TOKEN"]["value"] == "s3cret"
    assert request["initial_message"]["content"][0]["text"] == "hello"
    # A plain-string initial message should start the agent.
    assert request["initial_message"]["run"] is True


def test_start_request_can_override_the_llm_with_a_profile(client, authed_headers, fake_agent_server):
    client.post(
        "/api/v1/settings/profiles/fast",
        json={"llm": {"model": "gpt-4o-mini", "api_key": "sk-fast", "usage_id": "agent"}},
        headers=authed_headers,
    )

    task = client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hi", "llm_profile": "fast"},
        headers=authed_headers,
    ).json()

    request = fake_agent_server.state.conversations[task["app_conversation_id"]]
    assert request["agent"]["llm"]["model"] == "gpt-4o-mini"
    assert request["agent"]["llm"]["api_key"] == "sk-fast"

    # The override is per-conversation and must not be persisted.
    settings = client.get("/api/v1/settings", headers=authed_headers).json()
    assert settings["agent_settings"]["llm"]["model"] == TEST_MODEL


def test_start_request_rejects_an_unknown_profile(client, authed_headers):
    response = client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hi", "llm_profile": "nope"},
        headers=authed_headers,
    )
    assert response.status_code == 404


def test_persisted_conversation_settings_reach_the_runtime(client, authed_headers, fake_agent_server):
    client.post(
        "/api/v1/settings",
        json={"conversation_settings_diff": {"max_iterations": 7}},
        headers=authed_headers,
    )

    task = client.post(
        "/api/v1/app-conversations",
        json={"initial_message": "hi"},
        headers=authed_headers,
    ).json()

    request = fake_agent_server.state.conversations[task["app_conversation_id"]]
    assert request["max_iterations"] == 7


def test_no_sandbox_is_started_when_settings_are_missing(fake_agent_server, tmp_path, authed_headers):
    """The 400 must happen before a container is created, or we leak sandboxes."""
    from fastapi.testclient import TestClient

    from app_server.app import create_app
    from app_server.config import AppServerConfig

    started: list[str] = []

    class RecordingSandboxProvider:
        async def start_sandbox(self, sandbox_spec_id=None):
            started.append("start")
            raise AssertionError("sandbox should not be started without settings")

    app = create_app(
        AppServerConfig(session_api_keys=["app-secret"], state_dir=tmp_path),
        sandbox_service=RecordingSandboxProvider(),
    )
    with TestClient(app) as app_client:
        response = app_client.post(
            "/api/v1/app-conversations",
            json={"initial_message": "hi"},
            headers=authed_headers,
        )

    assert response.status_code == 400
    assert started == []

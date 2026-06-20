from __future__ import annotations


def test_sandbox_pause_resume(client, authed_headers):
    conversation = client.post(
        "/api/v1/app-conversations",
        json={"initial_message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        headers=authed_headers,
    ).json()
    sandbox_id = conversation["sandbox_id"]

    pause = client.post(f"/api/v1/sandboxes/{sandbox_id}/pause", headers=authed_headers)
    assert pause.status_code == 200
    assert pause.json()["status"] == "PAUSED"

    resume = client.post(f"/api/v1/sandboxes/{sandbox_id}/resume", headers=authed_headers)
    assert resume.status_code == 200
    assert resume.json()["status"] == "RUNNING"

class FakeSandboxProvider:
    def __init__(self, runtime_url: str):
        self.runtime_url = runtime_url
        self.started = 0
        self.paused = []
        self.resumed = []

    async def start_sandbox(self, sandbox_spec_id=None, sandbox_id=None):
        from app_server.models import Sandbox

        self.started += 1
        return Sandbox(
            id="provider-sandbox",
            agent_server_url=self.runtime_url,
            session_api_key="runtime-secret",
        )

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        self.paused.append(sandbox_id)
        return True

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        self.resumed.append(sandbox_id)
        return True


def test_app_conversation_uses_sandbox_provider(fake_agent_server, tmp_path, authed_headers):
    from fastapi.testclient import TestClient

    from app_server.app import create_app
    from app_server.config import AppServerConfig

    provider = FakeSandboxProvider(fake_agent_server.base_url)
    app = create_app(
        AppServerConfig(
            session_api_keys=["app-secret"],
            state_dir=tmp_path,
            public_base_url="http://app-server.test",
        ),
        sandbox_service=provider,
    )
    with TestClient(app) as app_client:
        conversation = app_client.post(
            "/api/v1/app-conversations",
            json={"initial_message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
            headers=authed_headers,
        ).json()
        assert provider.started == 1
        assert conversation["sandbox_id"] == "provider-sandbox"

        assert app_client.post(
            "/api/v1/sandboxes/provider-sandbox/pause", headers=authed_headers
        ).json()["success"] is True
        assert provider.paused == ["provider-sandbox"]
        assert app_client.post(
            "/api/v1/sandboxes/provider-sandbox/resume", headers=authed_headers
        ).json()["success"] is True
        assert provider.resumed == ["provider-sandbox"]

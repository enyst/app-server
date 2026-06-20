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

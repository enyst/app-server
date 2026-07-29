"""End-to-end tests against a real ``openhands-agent-server``.

The unit suite runs app_server against a *fake* agent-server that accepts any
JSON body. That proves the routing but not the one thing that actually breaks
on an SDK bump: whether the ``StartConversationRequest`` we build is *schema-valid*
against the real runtime. These tests close that gap by standing up an actual
agent-server subprocess (the same package version app_server targets) and
driving the real conversation-start, proxy, and send-message paths through it.

One server is shared across the module (it is slow to boot); each test uses its
own app_server state dir and conversation, so they stay independent. No LLM is
ever called — conversations are created idle and messages are sent with
``run=False`` — so no API key or network model access is needed.

Skipped automatically when ``openhands-agent-server`` (and its ``openhands-tools``
runtime dependency) are not importable, so the pure-unit CI job still passes.
Run explicitly with ``pytest tests/integration``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app_server.app import create_app
from app_server.config import AppServerConfig
from app_server.settings import Settings
from app_server.state import AppState

# The real runtime and its tool implementations must both be importable, or the
# server boots and then 500s on the first request (the terminal tool needs
# libtmux from openhands-tools).
pytest.importorskip("openhands.agent_server", reason="openhands-agent-server not installed")
pytest.importorskip("openhands.tools", reason="openhands-tools not installed")

pytestmark = pytest.mark.integration

RUNTIME_KEY = "itest-runtime-secret"
APP_KEY = "itest-app-secret"
TEST_MODEL = "gpt-4o"
TEST_LLM_API_KEY = "sk-integration-not-real"
BOOT_TIMEOUT_SECONDS = 90.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scrubbed_env(session_key: str) -> dict[str, str]:
    """A child env that can't inherit a stray session key from the shell/CI.

    Anything named ``OH_*`` (notably ``OH_SESSION_API_KEYS_0``) takes priority in
    the agent-server's config loader, so a value left in the developer's shell
    would silently override the key we set here and every request would 401.
    Strip them all and set exactly what we need.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OH_")}
    env.pop("SESSION_API_KEY", None)
    env["SESSION_API_KEY"] = session_key
    env["OPENHANDS_SUPPRESS_BANNER"] = "1"
    return env


@dataclass
class RunningAgentServer:
    base_url: str
    session_key: str

    def client(self) -> httpx.Client:
        """A direct httpx client to the runtime, for asserting propagated state."""
        return httpx.Client(
            base_url=self.base_url,
            headers={"X-Session-API-Key": self.session_key},
            timeout=30.0,
        )


@pytest.fixture(scope="module")
def real_agent_server() -> Iterator[RunningAgentServer]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    # Run in a throwaway cwd so the server's workspace/conversations/ state does
    # not litter the repo, and is discarded on teardown.
    work_dir = tempfile.mkdtemp(prefix="agent-server-itest-")
    log_path = Path(work_dir) / "agent-server.log"

    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "openhands.agent_server", "--host", "127.0.0.1", "--port", str(port)],
            cwd=work_dir,
            env=_scrubbed_env(RUNTIME_KEY),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    def _fail(message: str) -> None:
        proc.kill()
        proc.wait(timeout=10)
        log_tail = log_path.read_text()[-3000:] if log_path.exists() else "<no log>"
        raise RuntimeError(f"{message}\n--- agent-server log tail ---\n{log_tail}")

    try:
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        while True:
            if proc.poll() is not None:
                _fail(f"agent-server exited early with code {proc.returncode}")
            try:
                if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                _fail(f"agent-server did not become healthy within {BOOT_TIMEOUT_SECONDS:.0f}s")
            time.sleep(0.5)

        yield RunningAgentServer(base_url=base_url, session_key=RUNTIME_KEY)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _make_app_client(server: RunningAgentServer, tmp_path: Path, *, configured: bool = True) -> TestClient:
    """An app_server pointed at the real runtime via the static provider."""
    if configured:
        AppState(tmp_path).save_settings(
            Settings(
                agent_settings={
                    "llm": {"model": TEST_MODEL, "api_key": TEST_LLM_API_KEY, "usage_id": "agent"},
                }
            )
        )
    app = create_app(
        AppServerConfig(
            session_api_keys=[APP_KEY],
            state_dir=tmp_path,
            static_agent_server_url=server.base_url,
            static_agent_server_session_key=server.session_key,
            enable_websocket_gateway=True,
        )
    )
    return TestClient(app)


@pytest.fixture
def app_client(real_agent_server: RunningAgentServer, tmp_path: Path) -> Iterator[TestClient]:
    with _make_app_client(real_agent_server, tmp_path) as client:
        yield client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-Session-API-Key": APP_KEY}


def _start_conversation(client: TestClient, auth: dict[str, str], **body) -> str:
    response = client.post("/api/v1/app-conversations", json=body, headers=auth)
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "READY"
    return task["app_conversation_id"]


# ── Tests ───────────────────────────────────────────────────────────


def test_start_request_is_schema_valid_against_real_runtime(app_client, auth, real_agent_server):
    """The headline: our StartConversationRequest is accepted by the real server.

    A fake accepts any dict; the real agent-server validates against the SDK
    schema and would reject a malformed payload, so a 200 here is the proof the
    unit suite can't give.
    """
    conversation_id = _start_conversation(app_client, auth)

    # The persisted LLM actually reached the runtime's agent, not just our JSON.
    with real_agent_server.client() as runtime:
        detail = runtime.get(f"/api/conversations/{conversation_id}").json()
    assert detail["agent"]["llm"]["model"] == TEST_MODEL
    # No initial message -> the runtime stays idle; no LLM call happened.
    assert detail["execution_status"] == "idle"


def test_persisted_conversation_settings_reach_the_runtime(real_agent_server, tmp_path, auth):
    """max_iterations saved via /settings must land on the runtime conversation."""
    with _make_app_client(real_agent_server, tmp_path) as client:
        saved = client.post(
            "/api/v1/settings",
            json={"conversation_settings_diff": {"max_iterations": 7}},
            headers=auth,
        )
        assert saved.status_code == 200, saved.text

        conversation_id = _start_conversation(client, auth)

    with real_agent_server.client() as runtime:
        detail = runtime.get(f"/api/conversations/{conversation_id}").json()
    assert detail["max_iterations"] == 7


def test_send_message_and_event_proxy_flow(app_client, auth):
    """Start idle, then send a message through the proxy and see the runtime
    ingest it — exercising the event proxy against real event storage."""
    conversation_id = _start_conversation(app_client, auth)

    before = app_client.get(f"/api/conversations/{conversation_id}/events/count", headers=auth)
    assert before.status_code == 200
    assert before.json() == 0

    sent = app_client.post(
        f"/api/v1/app-conversations/{conversation_id}/send-message",
        json={"role": "user", "content": [{"type": "text", "text": "hello from the integration test"}], "run": False},
        headers=auth,
    )
    assert sent.status_code == 200, sent.text

    after = app_client.get(f"/api/conversations/{conversation_id}/events/count", headers=auth)
    assert after.status_code == 200
    assert after.json() > 0

    search = app_client.get(f"/api/v1/conversation/{conversation_id}/events/search", headers=auth)
    assert search.status_code == 200
    assert "hello from the integration test" in search.text


def test_custom_secrets_reach_the_runtime_as_env_vars(app_client, auth, real_agent_server):
    """A custom secret saved via /secrets is delivered to the conversation."""
    app_client.post(
        "/api/v1/secrets",
        json={"name": "MY_INTEGRATION_SECRET", "value": "s3cret-value"},
        headers=auth,
    )

    conversation_id = _start_conversation(app_client, auth)

    with real_agent_server.client() as runtime:
        detail = runtime.get(f"/api/conversations/{conversation_id}").json()
    # The runtime records the secret's *key* in its registry, never the value.
    assert "MY_INTEGRATION_SECRET" in str(detail["secret_registry"])
    assert "s3cret-value" not in str(detail)


def test_unconfigured_app_server_is_rejected_before_touching_runtime(real_agent_server, tmp_path, auth):
    """With no LLM configured, the start is a 400 and no runtime conversation is created."""
    with _make_app_client(real_agent_server, tmp_path, configured=False) as client:
        response = client.post("/api/v1/app-conversations", json={}, headers=auth)
    assert response.status_code == 400
    assert "settings" in response.json()["detail"].lower()

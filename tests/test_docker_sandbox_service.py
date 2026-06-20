"""Adapted non-enterprise Docker sandbox lifecycle tests from OpenHands/OpenHands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from docker.errors import APIError, NotFound

from app_server.models import AGENT_SERVER, VSCODE, SandboxPage, SandboxStatus
from app_server.sandbox import DockerSandboxService, ExposedPort, SandboxSpec, VolumeMount


@pytest.fixture
def mock_docker_client():
    return MagicMock()


@pytest.fixture
def mock_httpx_client():
    client = AsyncMock(spec=httpx.AsyncClient)
    response = AsyncMock()
    response.raise_for_status = MagicMock()
    client.get.return_value = response
    return client


@pytest.fixture
def sandbox_spec():
    return SandboxSpec(
        id="test-agent-server:latest",
        command=["--port", "8000"],
        initial_env={"TEST_VAR": "test_value"},
        working_dir="/workspace/project",
    )


@pytest.fixture
def service(mock_docker_client, mock_httpx_client, sandbox_spec):
    return DockerSandboxService(
        specs=[sandbox_spec],
        container_name_prefix="oh-test-",
        host_port=3000,
        container_url_pattern="http://localhost:{port}",
        mounts=[VolumeMount(host_path="/tmp/test", container_path="/workspace", mode="rw")],
        exposed_ports=[
            ExposedPort(name=AGENT_SERVER, description="Agent server", container_port=8000),
            ExposedPort(name=VSCODE, description="VSCode server", container_port=8001),
        ],
        health_check_path="/health",
        httpx_client=mock_httpx_client,
        max_num_sandboxes=3,
        docker_client=mock_docker_client,
    )


@pytest.fixture
def mock_running_container():
    container = MagicMock()
    container.name = "oh-test-abc123"
    container.status = "running"
    container.image.tags = ["test-agent-server:latest"]
    container.attrs = {
        "Created": "2024-01-15T10:30:00.000000000Z",
        "Config": {
            "Env": ["OH_SESSION_API_KEYS_0=session_key_123", "OTHER_VAR=other_value"],
            "WorkingDir": "/workspace/project",
        },
        "NetworkSettings": {
            "Ports": {
                "8000/tcp": [{"HostPort": "12345"}],
                "8001/tcp": [{"HostPort": "12346"}],
            }
        },
    }
    return container


@pytest.fixture
def mock_paused_container():
    container = MagicMock()
    container.name = "oh-test-def456"
    container.status = "paused"
    container.image.tags = ["test-agent-server:latest"]
    container.attrs = {
        "Created": "2024-01-15T10:30:00.000000000Z",
        "Config": {"Env": []},
        "NetworkSettings": {"Ports": {}},
    }
    return container


async def test_search_sandboxes_success(service, mock_running_container, mock_paused_container):
    service.docker_client.containers.list.return_value = [mock_running_container, mock_paused_container]
    service.httpx_client.get.return_value.raise_for_status.return_value = None

    result = await service.search_sandboxes()

    assert isinstance(result, SandboxPage)
    assert len(result.items) == 2
    running = next(item for item in result.items if item.status == SandboxStatus.RUNNING)
    assert running.id == "oh-test-abc123"
    assert running.sandbox_spec_id == "test-agent-server:latest"
    assert running.session_api_key == "session_key_123"
    assert len(running.exposed_urls or []) == 2
    assert (running.exposed_urls or [])[0].url == "http://localhost:12345"

    paused = next(item for item in result.items if item.status == SandboxStatus.PAUSED)
    assert paused.id == "oh-test-def456"
    assert paused.session_api_key is None
    assert paused.exposed_urls is None


async def test_search_sandboxes_pagination(service):
    containers = []
    for index in range(5):
        container = MagicMock()
        container.name = f"oh-test-container{index}"
        container.status = "running"
        container.image.tags = ["test-agent-server:latest"]
        container.attrs = {
            "Created": f"2024-01-{15 + index:02d}T10:30:00.000000000Z",
            "Config": {"Env": [f"OH_SESSION_API_KEYS_0=session_key_{index}"]},
            "NetworkSettings": {"Ports": {}},
        }
        containers.append(container)
    service.docker_client.containers.list.return_value = containers

    first_page = await service.search_sandboxes(limit=3)
    assert len(first_page.items) == 3
    assert first_page.next_page_id == "3"

    second_page = await service.search_sandboxes(page_id="3", limit=3)
    assert len(second_page.items) == 2
    assert second_page.next_page_id is None


async def test_search_sandboxes_handles_docker_api_error(service):
    service.docker_client.containers.list.side_effect = APIError("Docker daemon error")
    assert await service.search_sandboxes() == SandboxPage(items=[], next_page_id=None)


async def test_search_sandboxes_skips_tagless_container(service):
    container = MagicMock()
    container.name = "oh-test-tagless"
    container.status = "paused"
    container.image.tags = []
    container.attrs = {
        "Created": "2024-01-15T10:30:00.000000000Z",
        "Config": {"Env": []},
        "NetworkSettings": {"Ports": {}},
    }
    service.docker_client.containers.list.return_value = [container]
    assert (await service.search_sandboxes()).items == []


async def test_start_sandbox_creates_container_with_session_key(service, sandbox_spec):
    container = MagicMock()
    container.name = "oh-test-custom"
    container.status = "running"
    container.image.tags = [sandbox_spec.id]
    container.attrs = {
        "Created": "2024-01-15T10:30:00.000000000Z",
        "Config": {
            "Env": ["OH_SESSION_API_KEYS_0=generated", "TEST_VAR=test_value"],
            "WorkingDir": "/workspace/project",
        },
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": "12345"}]}},
    }
    service.docker_client.containers.run.return_value = container
    service.session_key_factory = lambda: "generated"
    service.id_factory = lambda: "custom"
    service.port_factory = lambda: 12345

    sandbox = await service.start_sandbox()

    assert sandbox.id == "oh-test-custom"
    kwargs = service.docker_client.containers.run.call_args.kwargs
    assert kwargs["image"] == sandbox_spec.id
    assert kwargs["command"] == sandbox_spec.command
    assert kwargs["name"] == "oh-test-custom"
    assert kwargs["environment"]["OH_SESSION_API_KEYS_0"] == "generated"
    assert kwargs["ports"] == {8000: 12345, 8001: 12345}
    assert kwargs["volumes"] == {"/tmp/test": {"bind": "/workspace", "mode": "rw"}}


async def test_get_sandbox_by_session_api_key(service, mock_running_container):
    service.docker_client.containers.list.return_value = [mock_running_container]
    sandbox = await service.get_sandbox_by_session_api_key("session_key_123")
    assert sandbox is not None
    assert sandbox.id == "oh-test-abc123"
    assert await service.get_sandbox_by_session_api_key("wrong") is None


async def test_lifecycle_methods(service):
    container = MagicMock()
    container.status = "running"
    service.docker_client.containers.get.return_value = container

    assert await service.pause_sandbox("oh-test-abc") is True
    container.pause.assert_called_once()

    container.status = "paused"
    assert await service.resume_sandbox("oh-test-abc") is True
    container.unpause.assert_called_once()

    container.status = "running"
    assert await service.delete_sandbox("oh-test-abc") is True
    container.stop.assert_called_once()
    container.remove.assert_called_once()

    service.docker_client.containers.get.side_effect = NotFound("missing")
    assert await service.pause_sandbox("oh-test-missing") is False

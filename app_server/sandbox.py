from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import docker
import httpx
from docker.errors import APIError, NotFound
from pydantic import BaseModel, ConfigDict

from .models import AGENT_SERVER, VSCODE, ExposedUrl, Sandbox, SandboxPage, SandboxStatus

SESSION_API_KEY_VARIABLE = "OH_SESSION_API_KEYS_0"
WEBHOOK_CALLBACK_VARIABLE = "OH_WEBHOOKS_0_BASE_URL"


class VolumeMount(BaseModel):
    host_path: str
    container_path: str
    mode: str = "rw"

    model_config = ConfigDict(frozen=True)


class ExposedPort(BaseModel):
    name: str
    description: str
    container_port: int = 8000

    model_config = ConfigDict(frozen=True)


class SandboxSpec(BaseModel):
    id: str
    command: list[str] | None = None
    initial_env: dict[str, str] = {}
    working_dir: str = "/workspace/project"


def _default_id() -> str:
    return os.urandom(16).hex()


def _default_key() -> str:
    return os.urandom(32).hex()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@dataclass
class DockerSandboxService:
    """Minimal Docker sandbox lifecycle service adapted from OpenHands app_server."""

    specs: list[SandboxSpec]
    container_name_prefix: str
    host_port: int
    container_url_pattern: str
    mounts: list[VolumeMount]
    exposed_ports: list[ExposedPort]
    health_check_path: str | None
    httpx_client: httpx.AsyncClient
    max_num_sandboxes: int
    docker_client: docker.DockerClient = field(default_factory=docker.from_env)
    id_factory: Callable[[], str] = _default_id
    session_key_factory: Callable[[], str] = _default_key
    port_factory: Callable[[], int] = _free_port

    def _status(self, docker_status: str) -> SandboxStatus:
        return {
            "running": SandboxStatus.RUNNING,
            "paused": SandboxStatus.PAUSED,
            "exited": SandboxStatus.PAUSED,
            "created": SandboxStatus.STARTING,
            "restarting": SandboxStatus.STARTING,
            "removing": SandboxStatus.MISSING,
            "dead": SandboxStatus.ERROR,
        }.get(docker_status.lower(), SandboxStatus.ERROR)

    def _env(self, container: Any) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for entry in container.attrs.get("Config", {}).get("Env", []) or []:
            if "=" in entry:
                key, value = entry.split("=", 1)
                result[key] = value
            else:
                result[entry] = None
        return result

    async def _container_to_sandbox(self, container: Any) -> Sandbox | None:
        if not getattr(container.image, "tags", None):
            return None
        status = self._status(container.status)
        created = container.attrs.get("Created", "")
        try:
            created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            created_at = datetime.now(UTC)

        session_key = None
        exposed_urls = None
        agent_server_url = ""
        if status == SandboxStatus.RUNNING:
            session_key = self._env(container).get(SESSION_API_KEY_VARIABLE)
            exposed_urls = []
            for container_port, host_bindings in (
                container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
            ).items():
                if not host_bindings:
                    continue
                exposed = next(
                    (
                        port
                        for port in self.exposed_ports
                        if container_port == f"{port.container_port}/tcp"
                    ),
                    None,
                )
                if not exposed:
                    continue
                host_port = int(host_bindings[0]["HostPort"])
                url = self.container_url_pattern.format(port=host_port)
                if exposed.name == VSCODE:
                    url += f"/?tkn={session_key}&folder={container.attrs['Config'].get('WorkingDir', '')}"
                if exposed.name == AGENT_SERVER:
                    agent_server_url = url
                exposed_urls.append(ExposedUrl(name=exposed.name, url=url, port=exposed.container_port))

        return Sandbox(
            id=container.name,
            status=status,
            agent_server_url=agent_server_url,
            session_api_key=session_key,
            created_at=created_at,
            sandbox_spec_id=container.image.tags[0],
            exposed_urls=exposed_urls,
        )

    async def _checked(self, container: Any) -> Sandbox | None:
        sandbox = await self._container_to_sandbox(container)
        if sandbox and self.health_check_path and sandbox.agent_server_url:
            try:
                response = await self.httpx_client.get(f"{sandbox.agent_server_url}{self.health_check_path}")
                response.raise_for_status()
            except Exception:
                sandbox.status = SandboxStatus.STARTING
                sandbox.exposed_urls = None
                sandbox.session_api_key = None
        return sandbox

    async def search_sandboxes(self, page_id: str | None = None, limit: int = 100) -> SandboxPage:
        try:
            containers = self.docker_client.containers.list(all=True)
        except APIError:
            return SandboxPage(items=[], next_page_id=None)
        sandboxes = []
        for container in containers:
            if container.name and container.name.startswith(self.container_name_prefix):
                sandbox = await self._checked(container)
                if sandbox:
                    sandboxes.append(sandbox)
        sandboxes.sort(key=lambda item: item.created_at, reverse=True)
        try:
            start = int(page_id) if page_id else 0
        except ValueError:
            start = 0
        end = start + limit
        return SandboxPage(
            items=sandboxes[start:end],
            next_page_id=str(end) if end < len(sandboxes) else None,
        )

    async def get_sandbox(self, sandbox_id: str) -> Sandbox | None:
        if not sandbox_id.startswith(self.container_name_prefix):
            return None
        try:
            return await self._checked(self.docker_client.containers.get(sandbox_id))
        except (NotFound, APIError):
            return None

    async def get_sandbox_by_session_api_key(self, session_api_key: str) -> Sandbox | None:
        try:
            containers = self.docker_client.containers.list(all=True)
        except (NotFound, APIError):
            return None
        for container in containers:
            if container.name and container.name.startswith(self.container_name_prefix):
                if self._env(container).get(SESSION_API_KEY_VARIABLE) == session_api_key:
                    return await self._checked(container)
        return None

    async def start_sandbox(self, sandbox_spec_id: str | None = None, sandbox_id: str | None = None) -> Sandbox:
        spec = self.specs[0] if sandbox_spec_id is None else next(
            (item for item in self.specs if item.id == sandbox_spec_id), None
        )
        if spec is None:
            raise ValueError("Sandbox Spec not found")
        short_id = sandbox_id or self.id_factory()
        container_name = f"{self.container_name_prefix}{short_id}"
        session_key = self.session_key_factory()
        env = dict(spec.initial_env)
        env[SESSION_API_KEY_VARIABLE] = session_key
        env[WEBHOOK_CALLBACK_VARIABLE] = f"http://host.docker.internal:{self.host_port}/api/v1/webhooks"
        ports = {port.container_port: self.port_factory() for port in self.exposed_ports}
        volumes = {
            mount.host_path: {"bind": mount.container_path, "mode": mount.mode}
            for mount in self.mounts
        }
        try:
            container = self.docker_client.containers.run(
                image=spec.id,
                command=spec.command,
                remove=False,
                name=container_name,
                environment=env,
                ports=ports,
                volumes=volumes,
                working_dir=spec.working_dir,
                labels={"sandbox_spec_id": spec.id},
                detach=True,
                init=True,
            )
        except APIError as exc:
            raise RuntimeError(f"Failed to start container: {exc}") from exc
        sandbox = await self._container_to_sandbox(container)
        assert sandbox is not None
        return sandbox

    async def resume_sandbox(self, sandbox_id: str) -> bool:
        if not sandbox_id.startswith(self.container_name_prefix):
            return False
        try:
            container = self.docker_client.containers.get(sandbox_id)
            if container.status == "paused":
                container.unpause()
            elif container.status == "exited":
                container.start()
            return True
        except (NotFound, APIError):
            return False

    async def pause_sandbox(self, sandbox_id: str) -> bool:
        if not sandbox_id.startswith(self.container_name_prefix):
            return False
        try:
            container = self.docker_client.containers.get(sandbox_id)
            if container.status == "running":
                container.pause()
            return True
        except (NotFound, APIError):
            return False

    async def delete_sandbox(self, sandbox_id: str) -> bool:
        if not sandbox_id.startswith(self.container_name_prefix):
            return False
        try:
            container = self.docker_client.containers.get(sandbox_id)
            if container.status in {"running", "paused"}:
                container.stop(timeout=10)
            container.remove()
            return True
        except (NotFound, APIError):
            return False

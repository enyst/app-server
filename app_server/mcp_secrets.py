"""Redaction-aware merging of submitted MCP config onto stored MCP config.

``GET /api/v1/settings`` returns MCP secrets redacted to ``**********`` (and
sometimes strips them entirely, since the redaction marker validates back to
``None``). A client that does GET -> edit -> POST would therefore persist the
mask as the real credential, silently breaking every configured MCP server.

Everything here exists to make that round-trip safe: a submitted value that is
still the redaction marker (or was dropped altogether) falls back to the stored
secret, while a genuinely changed value is honored as sent.

Ported from ``openhands/app_server/settings/settings_models.py`` in
OpenHands/OpenHands.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from openhands.sdk.mcp.config import MCPServer, dump_mcp_config
from openhands.sdk.settings import validate_agent_settings
from openhands.sdk.utils.pydantic_secrets import REDACTED_SECRET_VALUE

# Sentinel distinguishing "client sent the mask and we have nothing stored to
# restore" (drop the field) from "restored to None" (keep it).
_MISSING_SECRET = object()

# Fields on an MCP server entry that can carry credentials.
MCP_SECRET_FIELDS = ("headers", "env", "auth")

# Schema version the SDK migrations normalize an incoming mcp_config to.
_MCP_CONFIG_MIGRATION_SCHEMA_VERSION = 4


def _is_redacted(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value == REDACTED_SECRET_VALUE or bool(
        re.fullmatch(rf"Bearer\s+{re.escape(REDACTED_SECRET_VALUE)}", value, flags=re.IGNORECASE)
    )


def _has_redacted(value: object) -> bool:
    if _is_redacted(value):
        return True
    if isinstance(value, Mapping):
        return any(_has_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_redacted(item) for item in value)
    return False


def _restore(value: Any, existing: Any) -> Any:
    if _is_redacted(value):
        if isinstance(existing, str) and not _is_redacted(existing):
            return existing
        return _MISSING_SECRET
    if isinstance(value, dict):
        existing_dict = existing if isinstance(existing, Mapping) else {}
        restored = {}
        for key, item in value.items():
            restored_item = _restore(item, existing_dict.get(key))
            if restored_item is not _MISSING_SECRET:
                restored[key] = restored_item
        return restored
    return value


def _is_authorization_header(key: object) -> bool:
    return isinstance(key, str) and key.lower() == "authorization"


def _header_value(headers: object, key: object) -> object:
    if not isinstance(headers, Mapping):
        return None
    for existing_key, value in headers.items():
        if isinstance(key, str) and isinstance(existing_key, str) and existing_key.lower() == key.lower():
            return value
    return None


def _restore_headers(incoming: object, existing_server: Mapping[str, Any]) -> object:
    if not isinstance(incoming, Mapping):
        return incoming
    existing_headers = existing_server.get("headers")
    restored = dict(existing_headers) if _has_redacted(incoming) and isinstance(existing_headers, Mapping) else {}
    for key, value in incoming.items():
        existing_value = _header_value(existing_headers, key)
        # Drop any case-variant of the same header before re-adding it, so
        # "authorization" submitted over a stored "Authorization" doesn't
        # leave both behind.
        for existing_key in tuple(restored):
            if isinstance(existing_key, str) and isinstance(key, str) and existing_key.lower() == key.lower():
                restored.pop(existing_key)
        if _is_authorization_header(key) and _is_redacted(value) and existing_value is None:
            continue
        restored_value = _restore(value, existing_value)
        if restored_value is not _MISSING_SECRET:
            restored[key] = restored_value
    return restored


def _merge_auth(value: Mapping[str, Any], existing: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a submitted ``auth`` credential onto the stored one.

    The GET round-trip strips secret sub-fields entirely (the redaction marker
    validates back to ``None`` and is then dropped), so an unchanged bearer
    credential arrives as ``{"strategy": "bearer"}`` with no ``value`` at all —
    not as the ``**********`` sentinel ``_restore`` looks for. Keep every field
    the client actually sent, then carry over stored sub-fields it omitted;
    only secrets are ever stripped, so an omitted key is always a stored secret.
    """
    merged: dict[str, Any] = {}
    for key, item in value.items():
        existing_item = existing.get(key)
        if isinstance(item, Mapping):
            merged[key] = _merge_auth(item, existing_item if isinstance(existing_item, Mapping) else {})
            continue
        restored = _restore(item, existing_item)
        if restored is not _MISSING_SECRET:
            merged[key] = restored
    for key, item in existing.items():
        if key not in value:
            merged[key] = deepcopy(item)
    return merged


def _restore_auth(value: object, existing: object) -> object:
    # Same credential type: merge, so stripped sub-fields come back. A genuine
    # strategy switch is honored as submitted rather than merged.
    if (
        isinstance(value, Mapping)
        and isinstance(existing, Mapping)
        and value.get("strategy") == existing.get("strategy")
    ):
        return _merge_auth(value, existing)
    if _has_redacted(value) and existing is not None:
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            if value.get("strategy") != existing.get("strategy"):
                return deepcopy(existing)
        elif not isinstance(value, str) or not isinstance(existing, str):
            return deepcopy(existing)
    return _restore(value, existing)


def _restore_submitted(
    incoming_server: dict[str, Any],
    existing_server: Mapping[str, Any],
    submitted_fields: tuple[str, ...],
) -> None:
    for field in submitted_fields:
        existing_value = existing_server.get(field)
        if field == "headers":
            restored = _restore_headers(incoming_server[field], existing_server)
        elif field == "auth":
            restored = _restore_auth(incoming_server[field], existing_value)
        else:
            restored = _restore(incoming_server[field], existing_value)
        if restored is _MISSING_SECRET:
            incoming_server.pop(field)
        else:
            incoming_server[field] = restored


def _carry_omitted(
    incoming_server: dict[str, Any],
    existing_server: Mapping[str, Any],
    submitted_fields: tuple[str, ...],
) -> None:
    """Carry over stored secret fields the client omitted entirely."""
    if "env" not in submitted_fields and existing_server.get("env") is not None:
        incoming_server["env"] = existing_server["env"]

    if "headers" not in submitted_fields:
        existing_headers = existing_server.get("headers")
        if isinstance(existing_headers, Mapping):
            headers = dict(existing_headers)
            # A submitted `auth` owns the Authorization header; don't
            # resurrect a stale stored one alongside it.
            if "auth" in submitted_fields:
                headers = {key: value for key, value in headers.items() if not _is_authorization_header(key)}
            if headers:
                incoming_server["headers"] = headers

    if "auth" not in submitted_fields and existing_server.get("auth") is not None:
        submitted_headers = incoming_server.get("headers")
        has_plain_authorization = isinstance(submitted_headers, Mapping) and any(
            _is_authorization_header(key) and not _is_redacted(value) for key, value in submitted_headers.items()
        )
        if not has_plain_authorization:
            incoming_server["auth"] = existing_server["auth"]


def _server_map(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    servers = value.get("mcpServers", value)
    return servers if isinstance(servers, dict) else None


def normalize_mcp_config(value: Any) -> dict[str, MCPServer]:
    """Run a raw mcp_config payload through the SDK's migrations and validation."""
    settings = validate_agent_settings(
        {
            "schema_version": _MCP_CONFIG_MIGRATION_SCHEMA_VERSION,
            "mcp_config": {} if value is None else value,
        }
    )
    return settings.mcp_config


def _endpoint_identity(server: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Identity used to match an incoming server against a stored one.

    Secrets are bound to the endpoint they authenticate against, so a renamed
    server keeps its credentials only when the endpoint is unchanged.
    """
    url = server.get("url")
    if isinstance(url, str) and url:
        return ("url", url)
    command = server.get("command")
    if not isinstance(command, str) or not command:
        return None
    args = server.get("args")
    # Bind environment secrets to the full process invocation.
    return ("stdio", command, tuple(args) if isinstance(args, list) else (), server.get("cwd"))


def _matching_existing_server(
    name: str,
    incoming_server: Mapping[str, Any],
    incoming_servers: Mapping[str, Any],
    existing_servers: Mapping[str, Any],
) -> Mapping[str, Any]:
    incoming_endpoint = _endpoint_identity(incoming_server)
    if incoming_endpoint is None:
        return {}

    named_server = existing_servers.get(name)
    if isinstance(named_server, Mapping):
        return named_server if incoming_endpoint == _endpoint_identity(named_server) else {}

    # The server was renamed. Only carry secrets across when the match is
    # unambiguous: exactly one dropped stored server and one added incoming
    # server share this endpoint.
    candidates = [
        server
        for existing_name, server in existing_servers.items()
        if existing_name not in incoming_servers
        and isinstance(server, Mapping)
        and incoming_endpoint == _endpoint_identity(server)
    ]
    competing_updates = sum(
        1
        for incoming_name, server in incoming_servers.items()
        if incoming_name not in existing_servers
        and isinstance(server, Mapping)
        and incoming_endpoint == _endpoint_identity(server)
    )
    return candidates[0] if len(candidates) == competing_updates == 1 else {}


def preserve_redacted_mcp_secrets(value: Any, existing: Mapping[str, MCPServer] | None) -> Any:
    """Return ``value`` with redacted/omitted secrets restored from ``existing``."""
    incoming_value = deepcopy(value)
    incoming_servers = _server_map(incoming_value)
    if incoming_servers is None:
        return incoming_value

    # Round-trip the stored config through the same normalization the incoming
    # payload gets, so both sides use one shape (e.g. an Authorization header
    # already folded into `auth`) when compared.
    existing_dump = dump_mcp_config(
        normalize_mcp_config(dump_mcp_config(existing or {}, context={"expose_secrets": "plaintext"})),
        context={"expose_secrets": "plaintext"},
    )
    for name, incoming_server in incoming_servers.items():
        if not isinstance(name, str) or not isinstance(incoming_server, dict):
            continue
        existing_server = _matching_existing_server(name, incoming_server, incoming_servers, existing_dump)
        submitted_fields = tuple(field for field in MCP_SECRET_FIELDS if field in incoming_server)
        _restore_submitted(incoming_server, existing_server, submitted_fields)
        _carry_omitted(incoming_server, existing_server, submitted_fields)

    return incoming_value

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AppConversation, AppConversationStartTask, Sandbox


class AppState:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sandboxes: dict[str, Sandbox] = {}
        self.conversations: dict[str, AppConversation] = {}
        self.tasks: dict[str, AppConversationStartTask] = {}
        self._settings_path = self.state_dir / "settings.json"
        self._secrets_path = self.state_dir / "secrets.json"

    def settings(self) -> dict[str, Any]:
        if not self._settings_path.exists():
            return {"agent_settings": {}, "conversation_settings": {}, "app_preferences": {}}
        return json.loads(self._settings_path.read_text())

    def save_settings(self, data: dict[str, Any]) -> None:
        self._settings_path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def secrets(self) -> dict[str, dict[str, str | None]]:
        if not self._secrets_path.exists():
            return {}
        return json.loads(self._secrets_path.read_text())

    def save_secrets(self, data: dict[str, dict[str, str | None]]) -> None:
        self._secrets_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def deep_merge(base: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in diff.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

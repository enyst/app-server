from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AppConversation, AppConversationStartTask, Sandbox
from .settings import Settings
from .user_secrets import Secrets


class AppState:
    """File-backed persistence for everything app_server owns.

    Runtime state (sandboxes, conversations, start tasks) is held in memory and
    flushed on change; settings and secrets are read through on each access so
    an external edit to the state dir is picked up without a restart.
    """

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._sandboxes_path = self.state_dir / "sandboxes.json"
        self._conversations_path = self.state_dir / "conversations.json"
        self._tasks_path = self.state_dir / "start_tasks.json"
        self.sandboxes: dict[str, Sandbox] = self._load_models(self._sandboxes_path, Sandbox)
        self.conversations: dict[str, AppConversation] = self._load_models(self._conversations_path, AppConversation)
        self.tasks: dict[str, AppConversationStartTask] = self._load_models(self._tasks_path, AppConversationStartTask)
        self._settings_path = self.state_dir / "settings.json"
        self._secrets_path = self.state_dir / "secrets.json"

    def _load_models(self, path: Path, model_type):
        if not path.exists():
            return {}
        raw = json.loads(path.read_text())
        return {key: model_type.model_validate(value) for key, value in raw.items()}

    def _save_models(self, path: Path, values: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(
                {key: value.model_dump(mode="json") for key, value in values.items()},
                indent=2,
                sort_keys=True,
            )
        )

    def save_runtime_state(self) -> None:
        self._save_models(self._sandboxes_path, self.sandboxes)
        self._save_models(self._conversations_path, self.conversations)
        self._save_models(self._tasks_path, self.tasks)

    # ── Settings ────────────────────────────────────────────────────

    def load_settings(self) -> Settings | None:
        """Persisted settings, or None if the user has never saved any."""
        if not self._settings_path.exists():
            return None
        return Settings.model_validate(json.loads(self._settings_path.read_text()))

    def save_settings(self, settings: Settings) -> None:
        # expose_secrets: this file *is* the durable store, so masked values
        # would overwrite the real credentials with "**********".
        self._settings_path.write_text(settings.model_dump_json(indent=2, context={"expose_secrets": True}))

    # ── Secrets ─────────────────────────────────────────────────────

    def load_secrets(self) -> Secrets:
        if not self._secrets_path.exists():
            return Secrets()
        return Secrets.model_validate(json.loads(self._secrets_path.read_text()))

    def save_secrets(self, secrets: Secrets) -> None:
        self._secrets_path.write_text(secrets.model_dump_json(indent=2, context={"expose_secrets": True}))

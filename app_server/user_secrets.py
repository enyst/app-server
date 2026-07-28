"""User secrets: custom env-var secrets and git provider tokens.

Custom secrets are exported into the conversation as environment variables;
provider tokens authenticate git operations against a hosting provider.

Ported (and trimmed) from ``openhands/app_server/secrets/secrets_models.py`` in
OpenHands/OpenHands. The upstream ``MappingProxyType``/frozen-model machinery
existed to keep a shared multi-user store immutable; a single-user file store
doesn't need it, so plain dicts are used here.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, SecretStr, SerializationInfo, field_serializer, field_validator

# Custom secrets become environment variables in the sandbox, so the name has
# to be a valid shell identifier.
_ENV_VAR_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_env_var_name(value: str, field_name: str = "name") -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    value = value.strip()
    if not _ENV_VAR_PATTERN.match(value):
        raise ValueError(
            f"{field_name} must be a valid environment variable name "
            "(letters, digits and underscores; not starting with a digit)"
        )
    return value


class ProviderToken(BaseModel):
    """A git provider credential."""

    token: SecretStr | None = None
    host: str | None = None
    user_id: str | None = None

    @field_serializer("token")
    def _token_serializer(self, token: SecretStr | None, info: SerializationInfo) -> str | None:
        if token is None:
            return None
        context = info.context or {}
        return token.get_secret_value() if context.get("expose_secrets", False) else str(token)


class CustomSecret(BaseModel):
    """A user-defined secret exported as an environment variable."""

    secret: SecretStr
    description: str | None = None

    @field_serializer("secret")
    def _secret_serializer(self, secret: SecretStr, info: SerializationInfo) -> str:
        context = info.context or {}
        return secret.get_secret_value() if context.get("expose_secrets", False) else str(secret)


class Secrets(BaseModel):
    """Provider tokens and custom secrets for the single self-hosted user."""

    provider_tokens: dict[str, ProviderToken] = Field(default_factory=dict)
    custom_secrets: dict[str, CustomSecret] = Field(default_factory=dict)

    @field_validator("custom_secrets")
    @classmethod
    def _validate_secret_names(cls, value: dict[str, CustomSecret]) -> dict[str, CustomSecret]:
        for name in value:
            validate_env_var_name(name, field_name="secret name")
        return value

    def get_env_vars(self) -> dict[str, str]:
        """Custom secrets as plain ``name -> value`` environment variables."""
        return {name: secret.secret.get_secret_value() for name, secret in self.custom_secrets.items()}


class CustomSecretWithoutValue(BaseModel):
    """A custom secret as returned by the listing endpoint."""

    name: str
    description: str | None = None


class CustomSecretCreate(CustomSecretWithoutValue):
    """Request body for creating or updating a custom secret."""

    value: SecretStr

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return validate_env_var_name(value, field_name="secret name")


class ProviderTokenCreate(BaseModel):
    """Request body for setting a git provider token."""

    token: SecretStr
    host: str | None = None
    user_id: str | None = None


def provider_tokens_set(secrets: Secrets) -> dict[str, Any]:
    """``provider -> host`` for every provider that has a usable credential."""
    return {provider: token.host for provider, token in secrets.provider_tokens.items() if token.token or token.user_id}

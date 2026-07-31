"""Credential metadata, safe storage, and interactive recovery hooks.

Credential values must never be logged, returned in status output, or persisted
in the JSON application configuration.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import set_key

CredentialName = Literal[
    "tmdb",
    "opensubtitles-api",
    "opensubtitles-username",
    "opensubtitles-password",
    "gemini-primary",
    "gemini-paid",
]


@dataclass(frozen=True)
class CredentialSpec:
    """Public metadata for one locally stored credential."""

    name: CredentialName
    display_name: str
    environment_variable: str
    management_url: str
    secret: bool = True


CREDENTIAL_SPECS: dict[CredentialName, CredentialSpec] = {
    "tmdb": CredentialSpec(
        name="tmdb",
        display_name="TMDb API key",
        environment_variable="TMDB_API_KEY",
        management_url="https://www.themoviedb.org/settings/api",
    ),
    "opensubtitles-api": CredentialSpec(
        name="opensubtitles-api",
        display_name="OpenSubtitles API key",
        environment_variable="OPENSUBTITLES_API_KEY",
        management_url="https://www.opensubtitles.com/en/consumers",
    ),
    "opensubtitles-username": CredentialSpec(
        name="opensubtitles-username",
        display_name="OpenSubtitles username",
        environment_variable="OPENSUBTITLES_USERNAME",
        management_url="https://www.opensubtitles.com/",
        secret=False,
    ),
    "opensubtitles-password": CredentialSpec(
        name="opensubtitles-password",
        display_name="OpenSubtitles password",
        environment_variable="OPENSUBTITLES_PASSWORD",
        management_url="https://www.opensubtitles.com/",
    ),
    "gemini-primary": CredentialSpec(
        name="gemini-primary",
        display_name="Gemini primary API key",
        environment_variable="GEMINI_PRIMARY_API_KEY",
        management_url="https://aistudio.google.com/app/apikey",
    ),
    "gemini-paid": CredentialSpec(
        name="gemini-paid",
        display_name="Gemini paid/fallback API key",
        environment_variable="GEMINI_PAID_API_KEY",
        management_url="https://aistudio.google.com/app/apikey",
    ),
}


class ApiCredentialError(RuntimeError):
    """An absent or rejected credential that may be replaced by the user."""

    def __init__(
        self,
        credential: CredentialName,
        reason: str,
        *,
        status_code: int | None = None,
    ):
        self.credential = credential
        self.reason = reason
        self.status_code = status_code
        spec = CREDENTIAL_SPECS[credential]
        super().__init__(f"{spec.display_name}: {reason}")

    @property
    def management_url(self) -> str:
        return CREDENTIAL_SPECS[self.credential].management_url


class ApiServiceError(RuntimeError):
    """A non-credential provider error safe to display without request details."""

    def __init__(self, provider: str, status_code: int | None, reason: str):
        self.provider = provider
        self.status_code = status_code
        self.reason = reason
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{provider} service error{status}: {reason}")


CredentialRecoveryHandler = Callable[[ApiCredentialError], bool]
_recovery_handler: ContextVar[CredentialRecoveryHandler | None] = ContextVar(
    "credential_recovery_handler", default=None
)


def set_credential_recovery_handler(
    handler: CredentialRecoveryHandler | None,
) -> None:
    """Set the recovery callback for the current execution context."""

    _recovery_handler.set(handler)


def request_credential_recovery(error: ApiCredentialError) -> bool:
    """Ask the active UI to replace a credential, if interactive recovery exists."""

    handler = _recovery_handler.get()
    return bool(handler and handler(error))


def credential_is_configured(name: CredentialName) -> bool:
    """Return configuration status without exposing the value."""

    from mkv_episode_matcher.core.environment import load_environment_settings

    field_name = CREDENTIAL_SPECS[name].environment_variable.lower()
    return bool(getattr(load_environment_settings(), field_name))


def store_credential(
    name: CredentialName,
    value: str,
    *,
    dotenv_path: Path = Path(".env"),
) -> None:
    """Persist a user-supplied value to the ignored dotenv file and this process."""

    if not value.strip():
        raise ValueError("Credential value cannot be empty")

    spec = CREDENTIAL_SPECS[name]
    dotenv_path = dotenv_path.resolve()
    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    if not dotenv_path.exists():
        dotenv_path.touch()

    set_key(
        str(dotenv_path),
        spec.environment_variable,
        value,
        quote_mode="always",
    )
    os.environ[spec.environment_variable] = value


def migrate_credentials_from_json(
    config_path: Path,
    *,
    dotenv_path: Path = Path(".env"),
) -> list[CredentialName]:
    """Move recognized legacy JSON credentials without returning their values."""

    if not config_path.exists():
        return []

    data = json.loads(config_path.read_text(encoding="utf-8"))
    source = data.get("Config", data)
    if not isinstance(source, dict):
        return []

    field_names: dict[str, CredentialName] = {
        "tmdb_api_key": "tmdb",
        "open_subtitles_api_key": "opensubtitles-api",
        "open_subtitles_username": "opensubtitles-username",
        "open_subtitles_password": "opensubtitles-password",
    }
    migrated: list[CredentialName] = []
    changed = False
    for field, credential in field_names.items():
        if field not in source:
            continue
        changed = True
        value = source.pop(field)
        if isinstance(value, str) and value:
            store_credential(credential, value, dotenv_path=dotenv_path)
            migrated.append(credential)

    if changed:
        temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary_path, config_path)

    return migrated


def status_code_from_exception(error: Exception) -> int | None:
    """Extract an HTTP-like status code from common client exceptions."""

    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(error, "code", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def looks_like_authentication_error(error: Exception) -> bool:
    """Classify authentication failures without relying on one client library."""

    status_code = status_code_from_exception(error)
    if status_code in (401, 403):
        return True
    message = str(error).lower()
    markers = (
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid key",
        "authentication failed",
        "invalid credentials",
        "api key is invalid",
        "api key expired",
    )
    return any(marker in message for marker in markers)

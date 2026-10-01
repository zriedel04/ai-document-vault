"""Settings: a TOML file, overridden by environment variables, validated by Pydantic.

Precedence, highest first: environment variables (``AIVAULT_<SECTION>__<KEY>``, for example
``AIVAULT_SERVER__PORT=9000``), then the TOML file, then the defaults here. Unknown keys are an
error, so a typo in ``config.toml`` fails at start-up instead of being ignored. API keys never
belong here (see ``config.example.toml``).
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

CONFIG_ENV_VAR = "AIVAULT_CONFIG"
DEFAULT_CONFIG_FILE = "config.toml"
LOOPBACK_NAMES = frozenset({"localhost"})


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VaultSettings(_Section):
    # The vault folder lives outside the repository. Storage (a later change) requires it.
    path: Path | None = None


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("port", mode="before")
    @classmethod
    def _not_a_boolean(cls, port: object) -> object:
        if isinstance(port, bool):
            msg = "server.port must be a number"
            raise ValueError(msg)
        return port

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, host: str) -> str:
        """The server is local-only: refuse any address other than loopback."""
        if host in LOOPBACK_NAMES:
            return host
        try:
            is_loopback = ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            msg = (
                f"server.host must be a loopback address (127.0.0.1, ::1, localhost), not {host!r}"
            )
            raise ValueError(msg)
        return host


class LoggingSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIVAULT_", env_nested_delimiter="__", extra="forbid", frozen=True
    )

    vault: VaultSettings = VaultSettings()
    server: ServerSettings = ServerSettings()
    logging: LoggingSettings = LoggingSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment first (it wins), then the values read from the TOML file.
        return (env_settings, init_settings)


def load_settings(config_file: Path | None = None) -> Settings:
    """Load settings from ``config_file``, ``$AIVAULT_CONFIG`` or ``./config.toml`` if present."""
    if config_file is None and (from_env := os.environ.get(CONFIG_ENV_VAR)):
        config_file = Path(from_env)
        explicit = True
    else:
        explicit = config_file is not None
        config_file = config_file or Path(DEFAULT_CONFIG_FILE)

    data: dict[str, object] = {}
    if config_file.is_file():
        with config_file.open("rb") as handle:
            data = tomllib.load(handle)
    elif explicit:
        msg = f"config file not found: {config_file}"
        raise FileNotFoundError(msg)
    # Keys from the file are validated like any other input (unknown keys are an error).
    return Settings(**data)  # type: ignore[arg-type]

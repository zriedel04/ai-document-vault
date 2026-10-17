"""Settings: a TOML file, overridden by environment variables, validated by Pydantic.

Precedence, highest first: environment variables (``AIVAULT_<SECTION>__<KEY>``, for example
``AIVAULT_SERVER__PORT=9000``), then the TOML file, then the defaults here. Unknown keys are an
error, so a typo in ``config.toml`` fails at start-up instead of being ignored. API keys never
belong here (see ``config.example.toml``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from vault.ingest.extractors.text import FALLBACK_ENCODING, FALLBACK_ENCODINGS

CONFIG_ENV_VAR = "AIVAULT_CONFIG"
DEFAULT_CONFIG_FILE = "config.toml"
# Keep in step with the Host pattern in vault.core.security.
SERVER_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
        """The server is local-only: only the three spellings the Host check accepts.

        Another loopback address such as ``127.0.0.2`` binds fine, but every request to it would
        carry a ``Host`` header that the security middleware rejects (``vault.core.security.HOST``
        names the same three spellings), so it is refused here instead of starting an app nobody
        can use. ``[::1]`` is accepted as a spelling of ``::1`` and returned without the
        brackets, which is the form uvicorn and the socket layer expect.
        """
        bare = "::1" if host == "[::1]" else host
        if bare not in SERVER_HOSTS:
            msg = f"server.host must be one of 127.0.0.1, localhost or ::1, not {host!r}"
            raise ValueError(msg)
        return bare


class FrontendSettings(_Section):
    # The built frontend (``npm run build``). Unset means ``frontend/dist`` next to ``backend/``.
    dist: Path | None = None


class ProcessingSettings(_Section):
    # The Windows code page for a TXT or Markdown file that is neither UTF-8 nor marked with a byte
    # order mark (vault.ingest.extractors.text). Windows-1252 suits English and Western European
    # notes; Windows-1251 Cyrillic ones, cp932 Japanese, and so on.
    text_fallback_encoding: str = FALLBACK_ENCODING

    @field_validator("text_fallback_encoding")
    @classmethod
    def _windows_code_page(cls, encoding: str) -> str:
        if encoding not in FALLBACK_ENCODINGS:
            choices = ", ".join(sorted(FALLBACK_ENCODINGS))
            msg = f"processing.text_fallback_encoding must be one of {choices}"
            raise ValueError(msg)
        return encoding


class WorkerSettings(_Section):
    """How many jobs of each stage may run at once (vault.jobs.worker). The defaults are the plan's:
    extraction 2, OCR 1, embedding 1, AI enrichment 2."""

    extract: int = Field(default=2, ge=1, le=16)
    ocr: int = Field(default=1, ge=1, le=16)
    metadata: int = Field(default=1, ge=1, le=16)
    chunk: int = Field(default=1, ge=1, le=16)
    embed: int = Field(default=1, ge=1, le=16)
    enrich: int = Field(default=2, ge=1, le=16)

    @field_validator("*", mode="before")
    @classmethod
    def _not_a_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            msg = "a worker limit must be a number"
            raise ValueError(msg)
        return value

    def limits(self) -> dict[str, int]:
        return self.model_dump()


class LoggingSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIVAULT_", env_nested_delimiter="__", extra="forbid", frozen=True
    )

    vault: VaultSettings = VaultSettings()
    server: ServerSettings = ServerSettings()
    frontend: FrontendSettings = FrontendSettings()
    processing: ProcessingSettings = ProcessingSettings()
    workers: WorkerSettings = WorkerSettings()
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

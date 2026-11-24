"""Settings: a TOML file, overridden by environment variables, validated by Pydantic.

Precedence, highest first: environment variables (``AIVAULT_<SECTION>__<KEY>``, for example
``AIVAULT_SERVER__PORT=9000``), then the TOML file, then the defaults here. Unknown keys are an
error, so a typo in ``config.toml`` fails at start-up instead of being ignored. API keys never
belong here (see ``config.example.toml``).
"""

from __future__ import annotations

import os
import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from vault.ai.providers.base import MODEL_ID, PROVIDER_NAMES
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
    # Heavy parsing runs in a child process (vault.jobs.sandbox) that is killed after this many
    # seconds, and whose memory is capped at this many MiB (committed memory on Windows, address
    # space on Linux).
    sandbox_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    sandbox_memory_mib: int = Field(default=2048, ge=256, le=65536)

    @field_validator("sandbox_timeout_seconds", "sandbox_memory_mib", mode="before")
    @classmethod
    def _not_a_boolean(cls, value: object) -> object:
        if isinstance(value, bool):
            msg = "a sandbox limit must be a number"
            raise ValueError(msg)
        return value

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


class RetrievalSettings(_Section):
    """The numbers of hybrid retrieval (plan section 8, ADR 0008), tuned against the eval set.

    ``keyword_candidates`` and ``vector_candidates`` are the plan's K_kw and K_vec (50 each): how
    many hits of each list fusion reads. ``rrf_k`` is the k of Reciprocal Rank Fusion, a chunk's
    score being the sum over the lists of ``1 / (rrf_k + rank)``; the plan gives no value, so the
    default is 60, the value of the paper that introduced RRF (Cormack, Clarke and Buettcher, 2009).
    ``max_chunks_per_document`` is the plan's M (step 6), default 3 as section 8's context
    selection says. Used by ``vault.retrieval.fusion``.

    Each is a whole number and nothing else: ``true``, ``60.0`` and ``"60"`` are refused, because
    a TOML file can hold any of them by mistake. Environment variables can only hold text, so
    ``AIVAULT_RETRIEVAL__RRF_K=60`` is read by :class:`_EnvironmentWholeNumbers`, which accepts the
    plain decimal spelling only.
    """

    keyword_candidates: int = Field(default=50, ge=1, le=500, strict=True)
    vector_candidates: int = Field(default=50, ge=1, le=500, strict=True)
    rrf_k: int = Field(default=60, ge=0, le=1000, strict=True)
    max_chunks_per_document: int = Field(default=3, ge=1, le=100, strict=True)


# How an environment variable writes a whole number: ASCII digits, no sign, space or leading zero.
# Use fullmatch, which a trailing newline does not pass.
_DECIMAL: Final = re.compile(r"0|[1-9][0-9]{0,5}")
# Sections whose numbers are strict whole numbers, read from environment text by _DECIMAL.
_STRICT_SECTIONS: Final = ("retrieval",)


def _whole_number_from_text(value: object) -> object:
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        return int(value)
    return value  # anything else is left for the strict field to refuse


class _EnvironmentWholeNumbers(PydanticBaseSettingsSource):
    """The environment source, with the strict sections' decimal text read as whole numbers.

    Only this source converts: the TOML file and values passed in code must hold real numbers.
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = dict(self._inner())
        for name in _STRICT_SECTIONS:
            section = values.get(name)
            if isinstance(section, dict):
                values[name] = {
                    key: _whole_number_from_text(value) for key, value in section.items()
                }
        return values


# A price is US dollars per million tokens, from 0 to this, in steps of at most one millionth of a
# dollar (six decimal places), so every price is a whole number of micro-dollars per million tokens
# and the cost arithmetic (vault.ai.usage) is exact integer arithmetic.
MAX_PRICE_USD_PER_MTOK: Final = Decimal(1000)
PRICE_PLACES: Final = 6
_PRICE_STEP: Final = Decimal(1).scaleb(-PRICE_PLACES)


def _price(value: object) -> Decimal:
    """``value`` as an exact price: a TOML number (read as a ``Decimal``, see :func:`load_settings`)
    or a whole number, finite, from 0 to :data:`MAX_PRICE_USD_PER_MTOK`, with at most
    :data:`PRICE_PLACES` decimal places. A ``float`` is refused (it is binary, not the number the
    owner wrote), and so is text, a boolean and anything else."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        msg = "a price is a number written in config.toml (US dollars per million tokens)"
        raise ValueError(msg)
    price = Decimal(value)
    if not price.is_finite() or not Decimal(0) <= price <= MAX_PRICE_USD_PER_MTOK:
        msg = f"a price is from 0 to {MAX_PRICE_USD_PER_MTOK} US dollars per million tokens"
        raise ValueError(msg)
    if price != price.quantize(_PRICE_STEP):
        msg = f"a price has at most {PRICE_PLACES} decimal places"
        raise ValueError(msg)
    return price


class ModelPrice(_Section):
    """What one model costs, in US dollars per million tokens (plan section 9, ``[pricing]``).

    ``input`` is the price of input tokens read normally and ``cached_input`` of input tokens read
    from the provider's prompt cache (``Usage.cached``, a part of ``Usage.input``); ``output`` is
    the price of output tokens. All three are required: a missing one is an error, not zero.
    """

    input: Decimal
    cached_input: Decimal
    output: Decimal

    @field_validator("*", mode="before")
    @classmethod
    def _exact_price(cls, value: object) -> Decimal:
        return _price(value)


def _pricing(value: object) -> object:
    """The ``[pricing]`` table's two levels of keys: a provider of ``PROVIDER_NAMES``, then a model
    id as ``LLMRequest.model`` accepts it (checked with ``fullmatch``)."""
    if not isinstance(value, dict):
        return value  # Pydantic refuses it with its own error
    for provider, models in value.items():
        if provider not in PROVIDER_NAMES:
            msg = f"pricing has a table for each provider, one of {', '.join(PROVIDER_NAMES)}"
            raise ValueError(msg)
        if isinstance(models, dict):
            for model in models:
                if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
                    msg = "pricing names each model by its provider model id"
                    raise ValueError(msg)
    return value


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
    retrieval: RetrievalSettings = RetrievalSettings()
    # ``[pricing.<provider>."<model>"]`` tables (ModelPrice). A model without one has an unknown
    # cost in the usage ledger, never zero. Set in the file only: an environment value is text,
    # which a price refuses.
    pricing: dict[str, dict[str, ModelPrice]] = Field(default_factory=dict)
    logging: LoggingSettings = LoggingSettings()

    @field_validator("pricing", mode="before")
    @classmethod
    def _pricing_keys(cls, value: object) -> object:
        return _pricing(value)

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
        return (_EnvironmentWholeNumbers(env_settings), init_settings)


def resolve_config_file(config_file: Path | None = None) -> tuple[Path, bool]:
    """The file :func:`load_settings` reads, and whether it was named (then it must exist):
    ``config_file``, ``$AIVAULT_CONFIG`` or ``./config.toml``. The backup copies the same file."""
    if config_file is None and (from_env := os.environ.get(CONFIG_ENV_VAR)):
        return Path(from_env), True
    return config_file or Path(DEFAULT_CONFIG_FILE), config_file is not None


def load_settings(config_file: Path | None = None) -> Settings:
    """Load settings from ``config_file``, ``$AIVAULT_CONFIG`` or ``./config.toml`` if present."""
    config_file, explicit = resolve_config_file(config_file)

    data: dict[str, object] = {}
    if config_file.is_file():
        with config_file.open("rb") as handle:
            # Every TOML float is read as the exact decimal written (a price of 0.2 is 0.2, not
            # the binary float nearest to it). The other sections hold no floats, and their whole
            # numbers refuse a Decimal as they refused a float.
            data = tomllib.load(handle, parse_float=Decimal)
    elif explicit:
        msg = f"config file not found: {config_file}"
        raise FileNotFoundError(msg)
    # Keys from the file are validated like any other input (unknown keys are an error).
    return Settings(**data)  # type: ignore[arg-type]

"""Provider names to provider factories (plan section 9, ADR 0009).

The router (``vault.ai.routing``) says which provider *name* serves a task; this registry turns
the name into an :class:`~vault.ai.providers.base.LLMProvider`. ``fake`` and ``anthropic`` are
registered; the OpenAI adapter joins :data:`FACTORIES` when it exists, so until then a route to it
is valid config that cannot be used (:class:`ProviderUnavailableError`). Creating a provider reads
no key and makes no network call. The Anthropic adapter is created without a key for now, so every
call it gets is :attr:`~vault.ai.providers.base.ErrorKind.AUTHENTICATION` and sends nothing; the
step that wires the key store (``vault.ai.keys``) to the AI service gives it the key. Its module
(and with it the SDK) is imported only when the first one is created.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from vault.ai.providers.base import LLMProvider
from vault.ai.providers.fake import FakeProvider
from vault.ai.routing import PROVIDER_NAMES

type ProviderFactory = Callable[[], LLMProvider]


def _anthropic() -> LLMProvider:
    from vault.ai.providers.anthropic import AnthropicProvider

    return AnthropicProvider()


FACTORIES: Final[Mapping[str, ProviderFactory]] = MappingProxyType(
    {"fake": FakeProvider, "anthropic": _anthropic}
)


class ProviderUnavailableError(Exception):
    """A known provider name that this version has no adapter for. Fixed text, no name."""

    def __init__(self) -> None:
        super().__init__("this provider is not available in this version of the app")


def create_provider(name: str) -> LLMProvider:
    """A new provider for ``name``, one of ``routing.PROVIDER_NAMES``.

    A name outside that list is a ``ValueError`` (a route or override is checked before it gets
    here, so this is a programming error); a known name with no adapter yet is
    :class:`ProviderUnavailableError`. The provider must report the name it was registered under.
    """
    if not isinstance(name, str) or name not in PROVIDER_NAMES:
        msg = "not a known provider name"
        raise ValueError(msg)
    factory = FACTORIES.get(name)
    if factory is None:
        raise ProviderUnavailableError
    provider = factory()
    if provider.name != name:
        msg = "a registered provider reports another name than it is registered under"
        raise RuntimeError(msg)
    return provider

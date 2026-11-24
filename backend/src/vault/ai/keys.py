"""Cloud provider API keys in Windows Credential Manager (plan sections 9 and 10, ADR 0010).

One key per cloud provider name: every name in :data:`vault.ai.routing.PROVIDER_NAMES` except
``fake``, which needs none (:data:`KEY_PROVIDERS`). Nothing calls this module yet; the Settings
page, ``vault.ps1 configure`` and the provider adapters come next.

The rules:

- **Where.** A generic credential named ``AIVaultLocal/<provider>`` (:data:`TARGET_PREFIX`) in
  the current user's Credential Manager, persisted on this computer only
  (``CRED_PERSIST_LOCAL_MACHINE``, so it does not roam with a domain profile), with a fixed user
  name and comment. It is called through advapi32 (``CredWriteW``, ``CredReadW``,
  ``CredDeleteW``, ``CredFree``) with ``ctypes``, so no dependency is added (ADR 0020 says
  why not the plan's ``keyring``). The secret is stored as UTF-16-LE
  without a terminator, the encoding ``cmdkey`` and the Control Panel write, so a key entered
  there reads back here.
- **Shape.** A key is 1 to :data:`MAX_KEY_LENGTH` visible ASCII characters (``!`` to ``~``):
  blank text, white space (a pasted line break), control characters and non-ASCII letters are
  refused. The check is a ``fullmatch`` and its message never repeats the value. A stored secret
  is held to the same rule when it is read back (its size is bounded before it is copied, then
  it is decoded strictly and re-encoded to the same bytes), so a credential edited by another
  tool into something this module would not write is :class:`KeyErrorKind.DAMAGED`, not a key.
- **Secrecy.** A key is an :class:`ApiKey`, whose ``repr`` and ``str`` hide it, which refuses to
  be pickled or formatted, and whose text comes out only through :meth:`ApiKey.reveal` at the
  moment an adapter sends it. A key never goes into ``config.toml``, a log, the database, an API
  response or an exception: every error here is a :class:`KeyStoreError` whose message is fixed
  text naming only the provider (a name from :data:`KEY_PROVIDERS`, checked before it is used),
  raised ``from None``. A name outside that list is a ``ValueError`` that does not repeat it.
- **Other systems.** Credential Manager exists only on Windows. Elsewhere
  :func:`default_backend` returns a backend whose every call is
  :class:`KeyErrorKind.UNAVAILABLE`; tests use :class:`MemoryCredentials`.

A missing key is not an error: :meth:`KeyStore.get` returns ``None`` and :meth:`KeyStore.delete`
returns ``False``. The Python string of a key cannot be wiped; the ``ctypes`` buffers that carry
it to and from Windows are zeroed after each call.
"""

from __future__ import annotations

import hmac
import re
import sys
from enum import Enum
from typing import Final, NoReturn, Protocol, final

from vault.ai.routing import PROVIDER_NAMES

TARGET_PREFIX: Final = "AIVaultLocal/"
KEY_PROVIDERS: Final = frozenset(PROVIDER_NAMES - {"fake"})
MAX_KEY_LENGTH: Final = 512

# Visible ASCII only: no space, tab, line break, NUL or other control character, no non-ASCII.
_KEY_SHAPE: Final = re.compile(rf"[\x21-\x7e]{{1,{MAX_KEY_LENGTH}}}")
# A target prefix: a fixed product-style name and a slash (tests pass a unique one).
_PREFIX_SHAPE: Final = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}/")
# The largest secret a key can be, in the stored encoding (two bytes per character).
MAX_SECRET_BYTES: Final = 2 * MAX_KEY_LENGTH


class KeyErrorKind(Enum):
    INVALID = "invalid"  # the key given does not have a key's shape
    DAMAGED = "damaged"  # a stored credential is not a key this module would write
    UNAVAILABLE = "unavailable"  # no Credential Manager on this system
    FAILED = "failed"  # Windows refused the call


_MESSAGES: Final = {
    KeyErrorKind.INVALID: (
        "the API key for {provider} is not valid: it must be 1 to "
        f"{MAX_KEY_LENGTH} visible ASCII characters with no spaces or line breaks"
    ),
    KeyErrorKind.DAMAGED: "the stored API key for {provider} is damaged: enter it again",
    KeyErrorKind.UNAVAILABLE: (
        "the API key for {provider} cannot be stored or read: "
        "Windows Credential Manager is not available on this system"
    ),
    KeyErrorKind.FAILED: "Windows Credential Manager refused the request for the {provider} key",
}


class KeyStoreError(Exception):
    """A key could not be stored, read or deleted. Fixed text naming only the provider.

    ``winerror`` is the Windows error number for :attr:`KeyErrorKind.FAILED` (a number the
    system produced, never text), and ``None`` for the other kinds.
    """

    def __init__(self, kind: KeyErrorKind, provider: str, winerror: int | None = None) -> None:
        if provider not in KEY_PROVIDERS:  # pragma: no cover - callers check first
            provider = "this provider"
        super().__init__(_MESSAGES[kind].format(provider=provider))
        self.kind = kind
        self.provider = provider
        self.winerror = winerror


class BackendError(Exception):
    """Raised by a backend without a provider; :class:`KeyStore` turns it into a KeyStoreError."""

    def __init__(self, kind: KeyErrorKind, winerror: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.winerror = winerror


@final
class ApiKey:
    """An API key whose text is shown only by :meth:`reveal`.

    ``repr`` and ``str`` are ``ApiKey(<hidden>)``; formatting, pickling and copying through
    ``__reduce__`` are refused, so a key cannot reach a log, a JSON body or a file by accident.
    Two keys compare equal in constant time.
    """

    __slots__ = ("_value",)
    _value: str

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not _KEY_SHAPE.fullmatch(value):
            msg = (
                f"an API key must be 1 to {MAX_KEY_LENGTH} visible ASCII characters "
                "with no spaces or line breaks"
            )
            raise ValueError(msg) from None
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The key's text, for the one request header that needs it. Never log it."""
        return self._value

    def __setattr__(self, name: str, value: object) -> NoReturn:
        msg = "an API key cannot be changed"
        raise AttributeError(msg)

    def __repr__(self) -> str:
        return "ApiKey(<hidden>)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        msg = "an API key cannot be formatted"
        raise TypeError(msg)

    def __reduce__(self) -> NoReturn:
        msg = "an API key cannot be pickled or copied"
        raise TypeError(msg)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ApiKey):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("ascii"), other._value.encode("ascii"))

    def __hash__(self) -> int:
        return hash((ApiKey, len(self._value)))


class CredentialBackend(Protocol):
    """Where secrets are kept, by target name. Raises :class:`BackendError` on failure."""

    def write(self, target: str, secret: bytes) -> None:
        """Store ``secret`` under ``target``, replacing what was there."""

    def read(self, target: str) -> bytes | None:
        """The secret under ``target``, or ``None`` when there is none."""

    def delete(self, target: str) -> bool:
        """Remove ``target``; ``False`` when there was nothing to remove."""


class MemoryCredentials:
    """An in-memory :class:`CredentialBackend` for tests and other systems' development runs."""

    def __init__(self) -> None:
        self._secrets: dict[str, bytes] = {}

    def __repr__(self) -> str:
        return f"MemoryCredentials({len(self._secrets)} entries)"

    def write(self, target: str, secret: bytes) -> None:
        self._secrets[target] = bytes(secret)

    def read(self, target: str) -> bytes | None:
        return self._secrets.get(target)

    def delete(self, target: str) -> bool:
        return self._secrets.pop(target, None) is not None


class UnavailableCredentials:
    """The backend on a system without Credential Manager: every call is UNAVAILABLE."""

    def write(self, target: str, secret: bytes) -> None:
        raise BackendError(KeyErrorKind.UNAVAILABLE)

    def read(self, target: str) -> bytes | None:
        raise BackendError(KeyErrorKind.UNAVAILABLE)

    def delete(self, target: str) -> bool:
        raise BackendError(KeyErrorKind.UNAVAILABLE)


def encode_secret(key: ApiKey) -> bytes:
    """The stored form of ``key``: UTF-16-LE, no terminator (what ``cmdkey`` writes)."""
    return key.reveal().encode("utf-16-le")


def decode_secret(secret: bytes) -> ApiKey:
    """The key a stored secret holds; ``ValueError`` (fixed text) when it is not one.

    The size is bounded first; then the bytes must decode strictly, re-encode to exactly the
    same bytes, and fit :class:`ApiKey`'s shape.
    """
    msg = "a stored secret is not an API key"
    if not isinstance(secret, bytes) or not 0 < len(secret) <= MAX_SECRET_BYTES:
        raise ValueError(msg)
    if len(secret) % 2:
        raise ValueError(msg)
    try:
        text = secret.decode("utf-16-le")
    except UnicodeDecodeError:
        raise ValueError(msg) from None
    if text.encode("utf-16-le") != secret:  # pragma: no cover - strict UTF-16 round-trips
        raise ValueError(msg)
    try:
        return ApiKey(text)
    except ValueError:
        raise ValueError(msg) from None


class KeyStore:
    """One API key per provider in :data:`KEY_PROVIDERS`, kept in a :class:`CredentialBackend`."""

    def __init__(self, backend: CredentialBackend, *, prefix: str = TARGET_PREFIX) -> None:
        if not isinstance(prefix, str) or not _PREFIX_SHAPE.fullmatch(prefix):
            msg = "a target prefix must be a short name of letters, digits and . _ - and a slash"
            raise ValueError(msg)
        self._backend = backend
        self._prefix = prefix

    def __repr__(self) -> str:
        return f"KeyStore(prefix={self._prefix!r})"

    def target(self, provider: str) -> str:
        """The credential's target name for ``provider``."""
        return self._prefix + _checked_provider(provider)

    def set(self, provider: str, key: ApiKey | str) -> None:
        """Store ``key`` for ``provider``, replacing any key it had."""
        target = self.target(provider)
        if not isinstance(key, ApiKey):
            try:
                key = ApiKey(key)
            except ValueError:
                raise KeyStoreError(KeyErrorKind.INVALID, provider) from None
        try:
            self._backend.write(target, encode_secret(key))
        except BackendError as error:
            raise KeyStoreError(error.kind, provider, error.winerror) from None

    def get(self, provider: str) -> ApiKey | None:
        """The key stored for ``provider``, or ``None`` when none is."""
        target = self.target(provider)
        try:
            secret = self._backend.read(target)
        except BackendError as error:
            raise KeyStoreError(error.kind, provider, error.winerror) from None
        if secret is None:
            return None
        try:
            return decode_secret(secret)
        except ValueError:
            raise KeyStoreError(KeyErrorKind.DAMAGED, provider) from None

    def has(self, provider: str) -> bool:
        """Whether a usable key is stored for ``provider`` (a damaged one raises)."""
        return self.get(provider) is not None

    def delete(self, provider: str) -> bool:
        """Remove the key for ``provider``; ``False`` when there was none."""
        target = self.target(provider)
        try:
            return self._backend.delete(target)
        except BackendError as error:
            raise KeyStoreError(error.kind, provider, error.winerror) from None


def _checked_provider(provider: object) -> str:
    if not isinstance(provider, str) or provider not in KEY_PROVIDERS:
        msg = "not a provider that takes an API key"
        raise ValueError(msg)
    return provider


def default_backend() -> CredentialBackend:
    """Credential Manager on Windows; elsewhere a backend that reports it is unavailable."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return WindowsCredentials()
    return UnavailableCredentials()


if sys.platform == "win32":  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _CRED_TYPE_GENERIC: Final = 1
    _CRED_PERSIST_LOCAL_MACHINE: Final = 2
    _ERROR_NOT_FOUND: Final = 1168
    _USER_NAME: Final = "api-key"
    _COMMENT: Final = "AI Document Vault API key"

    class _Credential(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None

    class WindowsCredentials:
        """:class:`CredentialBackend` over the current user's Credential Manager (generic)."""

        def write(self, target: str, secret: bytes) -> None:
            if not 0 < len(secret) <= MAX_SECRET_BYTES:
                raise BackendError(KeyErrorKind.INVALID)
            blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
            try:
                credential = _Credential()
                credential.Type = _CRED_TYPE_GENERIC
                credential.TargetName = target
                credential.Comment = _COMMENT
                credential.CredentialBlobSize = len(secret)
                credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
                credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
                credential.UserName = _USER_NAME
                if not _advapi32.CredWriteW(ctypes.byref(credential), 0):
                    raise BackendError(KeyErrorKind.FAILED, ctypes.get_last_error())
            finally:
                ctypes.memset(blob, 0, len(secret))

        def read(self, target: str) -> bytes | None:
            pointer = ctypes.POINTER(_Credential)()
            if not _advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
                code = ctypes.get_last_error()
                if code == _ERROR_NOT_FOUND:
                    return None
                raise BackendError(KeyErrorKind.FAILED, code)
            try:
                credential = pointer.contents
                size = int(credential.CredentialBlobSize)
                # Bound the size before copying, so a huge blob written by another tool is
                # damage and not a large allocation.
                if size == 0 or size > MAX_SECRET_BYTES or not credential.CredentialBlob:
                    return b""
                return ctypes.string_at(credential.CredentialBlob, size)
            finally:
                if pointer:
                    credential = pointer.contents
                    if credential.CredentialBlob and credential.CredentialBlobSize:
                        ctypes.memset(credential.CredentialBlob, 0, credential.CredentialBlobSize)
                _advapi32.CredFree(pointer)

        def delete(self, target: str) -> bool:
            if _advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
                return True
            code = ctypes.get_last_error()
            if code == _ERROR_NOT_FOUND:
                return False
            raise BackendError(KeyErrorKind.FAILED, code)

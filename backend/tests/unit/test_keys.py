"""API key storage (``vault.ai.keys``; plan sections 9 and 10, ADR 0010).

Synthetic keys only. The Windows test writes, reads and deletes a real credential under a unique
target name and always removes it.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import pickle
import subprocess
import sys
import traceback
import uuid
from collections.abc import Iterator

import pytest

from vault import main as vault_main
from vault.ai import keys
from vault.ai.keys import (
    KEY_PROVIDERS,
    MAX_KEY_LENGTH,
    MAX_SECRET_BYTES,
    TARGET_PREFIX,
    ApiKey,
    BackendError,
    KeyErrorKind,
    KeyStore,
    KeyStoreError,
    MemoryCredentials,
    UnavailableCredentials,
    decode_secret,
    default_backend,
    encode_secret,
)
from vault.ai.routing import PROVIDER_NAMES
from vault.core.logging import SafeFormatter, log_event

# Shaped like a real key, so a leak is easy to search for. Built from parts so that a secret
# scanner does not take the synthetic value for a real one.
SECRET = "-".join(("sk", "test", "SYNTHETIC", "0123456789", "abcdefXYZ"))
OTHER = "-".join(("sk", "test", "OTHERSYNTHETIC", "9876543210"))


def everything_said_about(error: BaseException) -> str:
    return "".join(traceback.format_exception(error)) + repr(error) + str(error)


@pytest.fixture
def store() -> KeyStore:
    return KeyStore(MemoryCredentials())


def test_every_cloud_provider_takes_a_key_and_the_fake_does_not() -> None:
    assert PROVIDER_NAMES - {"fake"} == KEY_PROVIDERS
    assert {"anthropic", "openai"} <= KEY_PROVIDERS
    with pytest.raises(ValueError, match="not a provider that takes an API key"):
        KeyStore(MemoryCredentials()).target("fake")


@pytest.mark.parametrize("provider", sorted(KEY_PROVIDERS))
def test_set_get_replace_and_delete(store: KeyStore, provider: str) -> None:
    assert store.get(provider) is None
    assert not store.has(provider)
    assert store.delete(provider) is False
    store.set(provider, SECRET)
    assert store.get(provider) == ApiKey(SECRET)
    assert store.has(provider)
    store.set(provider, ApiKey(OTHER))
    key = store.get(provider)
    assert key is not None
    assert key.reveal() == OTHER
    assert store.delete(provider) is True
    assert store.get(provider) is None


def test_providers_are_kept_apart(store: KeyStore) -> None:
    store.set("anthropic", SECRET)
    store.set("openai", OTHER)
    assert store.get("anthropic") == ApiKey(SECRET)
    assert store.get("openai") == ApiKey(OTHER)
    store.delete("anthropic")
    assert store.get("openai") == ApiKey(OTHER)


def test_target_names_use_the_fixed_prefix() -> None:
    backend = MemoryCredentials()
    store = KeyStore(backend)
    store.set("anthropic", SECRET)
    assert TARGET_PREFIX == "AIVaultLocal/"
    assert store.target("openai") == "AIVaultLocal/openai"
    assert backend.read("AIVaultLocal/anthropic") == SECRET.encode("utf-16-le")


@pytest.mark.parametrize(
    "provider", ["", "Anthropic", "anthropic\n", " openai", "fake", "local", "../x", None, 3]
)
def test_unknown_providers_are_refused_without_repeating_them(
    store: KeyStore, provider: object
) -> None:
    for call in (
        lambda: store.set(provider, SECRET),  # type: ignore[arg-type]
        lambda: store.get(provider),  # type: ignore[arg-type]
        lambda: store.delete(provider),  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError, match=r"^not a provider that takes an API key$") as caught:
            call()
        assert SECRET not in everything_said_about(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "\t",
        SECRET + "\n",
        SECRET + "\r",
        SECRET + " ",
        " " + SECRET,
        "sk-test SYNTHETIC",
        "sk-test\x00SYNTHETIC",
        "sk-test\x7fSYNTHETIC",
        "sk-test" + chr(0xE9) + "SYNTHETIC",
        "sk-test" + chr(0x200B) + "SYNTHETIC",
        "sk-test" + chr(0xA0) + "SYNTHETIC",
        "x" * (MAX_KEY_LENGTH + 1),
    ],
)
def test_badly_shaped_keys_are_refused_without_echoing_them(store: KeyStore, value: str) -> None:
    with pytest.raises(ValueError, match="an API key must be") as built:
        ApiKey(value)
    assert value.strip() == "" or value not in everything_said_about(built.value)
    assert built.value.__cause__ is None
    with pytest.raises(KeyStoreError) as stored:
        store.set("anthropic", value)
    assert stored.value.kind is KeyErrorKind.INVALID
    assert stored.value.provider == "anthropic"
    assert "anthropic" in str(stored.value)
    said = everything_said_about(stored.value)
    assert value.strip() == "" or value not in said
    assert "SYNTHETIC" not in said
    assert store.get("anthropic") is None


@pytest.mark.parametrize("value", [None, 12, b"sk-test-bytes", ["sk"]])
def test_a_key_must_be_text(value: object) -> None:
    with pytest.raises(ValueError, match="an API key must be"):
        ApiKey(value)  # type: ignore[arg-type]


def test_the_longest_key_and_every_visible_character_are_accepted(store: KeyStore) -> None:
    longest = "y" * MAX_KEY_LENGTH
    store.set("openai", longest)
    assert store.get("openai") == ApiKey(longest)
    visible = "".join(chr(code) for code in range(0x21, 0x7F))
    store.set("openai", visible)
    assert store.get("openai") == ApiKey(visible)
    assert len(encode_secret(ApiKey(longest))) == MAX_SECRET_BYTES


def test_a_key_hides_its_text() -> None:
    key = ApiKey(SECRET)
    assert repr(key) == "ApiKey(<hidden>)"
    assert str(key) == "ApiKey(<hidden>)"
    assert "%s %r" % (key, key) == "ApiKey(<hidden>) ApiKey(<hidden>)"  # noqa: UP031
    assert SECRET not in repr([key, {"k": key}])
    with pytest.raises(TypeError, match="cannot be formatted"):
        f"{key}"
    with pytest.raises(TypeError, match="cannot be formatted"):
        "{}".format(key)  # noqa: UP032
    for leak in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError) as caught:
            leak(key)
        assert SECRET not in everything_said_about(caught.value)
    with pytest.raises(AttributeError):
        key._value = OTHER
    assert not hasattr(key, "__dict__")
    assert key.reveal() == SECRET


def test_keys_compare_by_value() -> None:
    assert ApiKey(SECRET) == ApiKey(SECRET)
    assert ApiKey(SECRET) != ApiKey(OTHER)
    assert ApiKey(SECRET) != SECRET
    assert hash(ApiKey(SECRET)) == hash(ApiKey(SECRET))


def test_a_logged_key_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logger = logging.getLogger(vault_main.__name__)
    log_event(logger, "ai.key_set", key=ApiKey(SECRET))  # type: ignore[arg-type]
    logger.info("plain %s", ApiKey(SECRET))
    lines = [SafeFormatter().format(record) for record in caplog.records]
    assert lines
    assert all(SECRET not in line for line in lines)


@pytest.mark.parametrize(
    "secret",
    [
        b"",
        b"s",  # odd length
        "sk-test bad".encode("utf-16-le"),
        (SECRET + "\x00").encode("utf-16-le"),  # a terminator is not part of a key
        SECRET.encode("utf-8"),  # another tool's encoding
        "\ud800".encode("utf-16-le", "surrogatepass"),  # a lone surrogate
        ("z" * (MAX_KEY_LENGTH + 1)).encode("utf-16-le"),
        b"\x00" * (MAX_SECRET_BYTES + 2),
    ],
)
def test_a_stored_secret_that_is_not_a_key_is_damage(secret: bytes) -> None:
    backend = MemoryCredentials()
    store = KeyStore(backend)
    backend.write(store.target("anthropic"), secret)
    with pytest.raises(KeyStoreError) as caught:
        store.get("anthropic")
    assert caught.value.kind is KeyErrorKind.DAMAGED
    assert str(caught.value) == "the stored API key for anthropic is damaged: enter it again"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    with pytest.raises(KeyStoreError):
        store.has("anthropic")
    assert store.delete("anthropic") is True


def test_decode_refuses_what_is_not_bytes() -> None:
    with pytest.raises(ValueError, match="not an API key"):
        decode_secret(SECRET)  # type: ignore[arg-type]


def test_secrets_round_trip_in_the_cmdkey_encoding() -> None:
    key = ApiKey(SECRET)
    assert encode_secret(key) == SECRET.encode("utf-16-le")
    assert decode_secret(encode_secret(key)) == key


class FailingCredentials:
    def write(self, target: str, secret: bytes) -> None:
        raise BackendError(KeyErrorKind.FAILED, 5)

    def read(self, target: str) -> bytes | None:
        raise BackendError(KeyErrorKind.FAILED, 5)

    def delete(self, target: str) -> bool:
        raise BackendError(KeyErrorKind.FAILED, 5)


def test_a_backend_failure_is_fixed_text_naming_the_provider() -> None:
    store = KeyStore(FailingCredentials())
    for call in (
        lambda: store.set("openai", SECRET),
        lambda: store.get("openai"),
        lambda: store.delete("openai"),
    ):
        with pytest.raises(KeyStoreError) as caught:
            call()
        assert caught.value.kind is KeyErrorKind.FAILED
        assert caught.value.winerror == 5
        assert str(caught.value) == (
            "Windows Credential Manager refused the request for the openai key"
        )
        assert SECRET not in everything_said_about(caught.value)


def test_unavailable_store_says_so_for_every_call() -> None:
    store = KeyStore(UnavailableCredentials())
    for call in (
        lambda: store.set("anthropic", SECRET),
        lambda: store.get("anthropic"),
        lambda: store.delete("anthropic"),
    ):
        with pytest.raises(KeyStoreError) as caught:
            call()
        assert caught.value.kind is KeyErrorKind.UNAVAILABLE
        assert caught.value.winerror is None
        assert "Credential Manager is not available" in str(caught.value)
        assert SECRET not in everything_said_about(caught.value)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has Credential Manager")
def test_other_systems_have_no_store() -> None:
    assert isinstance(default_backend(), UnavailableCredentials)
    with pytest.raises(KeyStoreError) as caught:
        KeyStore(default_backend()).get("anthropic")
    assert caught.value.kind is KeyErrorKind.UNAVAILABLE


@pytest.mark.parametrize(
    "prefix", ["", "AIVaultLocal", "AIVaultLocal/\n", "/x/", "a b/", "x" * 70 + "/", None]
)
def test_a_prefix_must_be_a_short_name_and_a_slash(prefix: object) -> None:
    with pytest.raises(ValueError, match="target prefix"):
        KeyStore(MemoryCredentials(), prefix=prefix)  # type: ignore[arg-type]


def test_reprs_hold_no_key(store: KeyStore) -> None:
    backend = MemoryCredentials()
    KeyStore(backend).set("anthropic", SECRET)
    assert SECRET not in repr(backend)
    assert SECRET not in repr(store)
    assert repr(UnavailableCredentials())


@pytest.fixture
def windows_store() -> Iterator[KeyStore]:
    store = KeyStore(default_backend(), prefix=f"AIVaultLocalTest-{uuid.uuid4().hex}/")
    try:
        yield store
    finally:
        for provider in KEY_PROVIDERS:
            with contextlib.suppress(KeyStoreError):
                store.delete(provider)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_credential_manager_round_trip(windows_store: KeyStore) -> None:  # pragma: no cover
    store = windows_store
    assert type(default_backend()).__name__ == "WindowsCredentials"
    assert store.get("anthropic") is None
    assert store.delete("anthropic") is False
    store.set("anthropic", SECRET)
    assert store.get("anthropic") == ApiKey(SECRET)
    store.set("anthropic", OTHER)
    assert store.get("anthropic") == ApiKey(OTHER)
    longest = "q" * MAX_KEY_LENGTH
    store.set("openai", longest)
    assert store.get("openai") == ApiKey(longest)
    assert store.get("anthropic") == ApiKey(OTHER)
    assert store.delete("anthropic") is True
    assert store.get("anthropic") is None
    assert store.delete("anthropic") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_reads_a_key_entered_with_cmdkey(
    windows_store: KeyStore,
) -> None:  # pragma: no cover
    target = windows_store.target("openai")
    subprocess.run(  # noqa: S603 - fixed program, synthetic arguments
        ["cmdkey", f"/generic:{target}", "/user:api-key", f"/pass:{SECRET}"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    assert windows_store.get("openai") == ApiKey(SECRET)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_reports_a_damaged_credential(windows_store: KeyStore) -> None:  # pragma: no cover
    backend = default_backend()
    target = windows_store.target("anthropic")
    backend.write(target, SECRET.encode("utf-8"))  # odd-length or wrong encoding
    with pytest.raises(KeyStoreError) as caught:
        windows_store.get("anthropic")
    assert caught.value.kind is KeyErrorKind.DAMAGED
    with pytest.raises(BackendError):
        backend.write(target, b"")
    assert keys.MAX_SECRET_BYTES == 2 * MAX_KEY_LENGTH

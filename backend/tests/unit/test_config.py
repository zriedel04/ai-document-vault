from pathlib import Path

import pytest
from pydantic import ValidationError

from vault.core.config import CONFIG_ENV_VAR, Settings, load_settings
from vault.core.security import HOST


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("AIVAULT_CONFIG", "AIVAULT_SERVER__PORT", "AIVAULT_SERVER__HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray config.toml


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file() -> None:
    settings = load_settings()
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8765
    assert settings.logging.level == "INFO"
    assert settings.vault.path is None
    assert settings.frontend.dist is None


def test_toml_file_values(tmp_path: Path) -> None:
    config = write(
        tmp_path / "c.toml",
        '[server]\nport = 9001\n[logging]\nlevel = "DEBUG"\n[vault]\npath = "/data/vault"\n',
    )
    settings = load_settings(config)
    assert settings.server.port == 9001
    assert settings.logging.level == "DEBUG"
    assert settings.vault.path == Path("/data/vault")
    assert settings.server.host == "127.0.0.1"  # untouched keys keep their defaults


def test_environment_overrides_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write(tmp_path / "c.toml", "[server]\nport = 9001\n")
    monkeypatch.setenv("AIVAULT_SERVER__PORT", "9002")
    assert load_settings(config).server.port == 9002


def test_environment_override_keeps_other_keys_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(tmp_path / "c.toml", '[server]\nport = 9001\n[logging]\nlevel = "ERROR"\n')
    monkeypatch.setenv("AIVAULT_SERVER__PORT", "9002")
    settings = load_settings(config)
    assert (settings.server.port, settings.logging.level) == (9002, "ERROR")


def test_config_path_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write(tmp_path / "elsewhere.toml", "[server]\nport = 9003\n")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))
    assert load_settings().server.port == 9003


def test_config_toml_in_the_current_folder_is_used(tmp_path: Path) -> None:
    write(tmp_path / "config.toml", "[server]\nport = 9004\n")
    assert load_settings().server.port == 9004


def test_explicit_missing_file_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.toml")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        load_settings()


@pytest.mark.parametrize(
    "text",
    [
        "[server]\nprt = 1\n",  # typo in a key
        "[serverr]\nport = 1\n",  # typo in a section
        "[server]\nport = 0\n",
        "[server]\nport = 70000\n",
        '[logging]\nlevel = "LOUD"\n',
        "[server]\nport = true\n",
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path / "c.toml", text))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("::1", "::1"),
        ("[::1]", "::1"),  # brackets are dropped: uvicorn and sockets want the bare address
        ("localhost", "localhost"),
    ],
)
def test_loopback_hosts_are_accepted_in_the_form_the_server_needs(host: str, expected: str) -> None:
    assert Settings.model_validate({"server": {"host": host}}).server.host == expected


def test_bracketed_host_reaches_uvicorn_without_brackets() -> None:
    settings = Settings.model_validate({"server": {"host": "[::1]"}})
    assert settings.server.host == "::1"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.5", "example.com", "", "8.8.8.8", "[::", "::1]", "[127.0.0.1"],  # noqa: S104
)
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"server": {"host": host}})


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.2",
        "127.1",
        "0:0:0:0:0:0:0:1",
        "LOCALHOST",
        "localhost ",
        "[localhost]",
        "[127.0.0.1]",
    ],
)
def test_a_loopback_address_the_host_check_would_reject_is_refused(host: str) -> None:
    # They bind, but requests to them carry a Host header the security middleware rejects.
    with pytest.raises(ValidationError):
        Settings.model_validate({"server": {"host": host}})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_every_accepted_host_is_accepted_by_the_host_check(host: str) -> None:
    bare = Settings.model_validate({"server": {"host": host}}).server.host
    spelled = f"[{bare}]" if ":" in bare else bare
    assert HOST.fullmatch(f"{spelled}:8765")


def test_environment_cannot_set_a_non_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIVAULT_SERVER__HOST", "0.0.0.0")  # noqa: S104
    with pytest.raises(ValidationError):
        load_settings()


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.server.port = 1  # type: ignore[misc]


def test_the_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[3] / "config.example.toml"
    settings = load_settings(example)
    assert settings.server.port == 8765
    assert settings.frontend.dist is None  # the example leaves the default in place

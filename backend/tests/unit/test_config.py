import tomllib
from decimal import Decimal
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


@pytest.mark.parametrize("name", ["config.example.toml", "config.demo.toml"])
def test_committed_config_files_load(name: str) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    assert load_settings(repo_root / name).server.host == "127.0.0.1"


def test_demo_config_does_not_collide_with_the_default_port() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    assert load_settings(repo_root / "config.demo.toml").server.port != Settings().server.port


def test_the_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[3] / "config.example.toml"
    settings = load_settings(example)
    assert settings.server.port == 8765
    assert settings.frontend.dist is None  # the example leaves the default in place


def test_the_text_fallback_defaults_to_windows_1252_and_can_be_chosen(tmp_path: Path) -> None:
    assert Settings().processing.text_fallback_encoding == "cp1252"
    config = tmp_path / "config.toml"
    config.write_text('[processing]\ntext_fallback_encoding = "cp1251"\n', encoding="utf-8")
    assert load_settings(config).processing.text_fallback_encoding == "cp1251"


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1", "CP1251", "cp1251 ", "koi8_r", ""])
def test_the_text_fallback_must_be_a_windows_code_page(tmp_path: Path, encoding: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f'[processing]\ntext_fallback_encoding = "{encoding}"\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="text_fallback_encoding"):
        load_settings(config)


RETRIEVAL_KEYS = (
    "keyword_candidates",
    "vector_candidates",
    "rrf_k",
    "max_chunks_per_document",
    "context_budget_tokens",
    "context_neighbor_chunks",
)


@pytest.fixture
def _no_retrieval_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in RETRIEVAL_KEYS:
        monkeypatch.delenv(f"AIVAULT_RETRIEVAL__{key.upper()}", raising=False)
    monkeypatch.delenv("AIVAULT_RETRIEVAL", raising=False)


@pytest.mark.usefixtures("_no_retrieval_environment")
def test_retrieval_defaults_are_the_plans() -> None:
    retrieval = Settings().retrieval
    assert (
        retrieval.keyword_candidates,
        retrieval.vector_candidates,
        retrieval.rrf_k,
        retrieval.max_chunks_per_document,
        retrieval.context_budget_tokens,
        retrieval.context_neighbor_chunks,
    ) == (50, 50, 60, 3, 8000, 1)


@pytest.mark.usefixtures("_no_retrieval_environment")
def test_retrieval_values_from_the_file(tmp_path: Path) -> None:
    config = write(
        tmp_path / "c.toml",
        "[retrieval]\nkeyword_candidates = 1\nvector_candidates = 500\nrrf_k = 0\n"
        "max_chunks_per_document = 100\ncontext_budget_tokens = 200000\n"
        "context_neighbor_chunks = 0\n",
    )
    retrieval = load_settings(config).retrieval
    assert (
        retrieval.keyword_candidates,
        retrieval.vector_candidates,
        retrieval.rrf_k,
        retrieval.max_chunks_per_document,
        retrieval.context_budget_tokens,
        retrieval.context_neighbor_chunks,
    ) == (1, 500, 0, 100, 200000, 0)
    other = write(
        tmp_path / "d.toml",
        "[retrieval]\ncontext_budget_tokens = 1000\ncontext_neighbor_chunks = 3\n",
    )
    retrieval = load_settings(other).retrieval
    assert (retrieval.context_budget_tokens, retrieval.context_neighbor_chunks) == (1000, 3)


@pytest.mark.usefixtures("_no_retrieval_environment")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("keyword_candidates", "0"),
        ("keyword_candidates", "501"),
        ("vector_candidates", "0"),
        ("vector_candidates", "501"),
        ("rrf_k", "-1"),
        ("rrf_k", "1001"),
        ("max_chunks_per_document", "0"),
        ("max_chunks_per_document", "101"),
        ("context_budget_tokens", "999"),
        ("context_budget_tokens", "200001"),
        ("context_neighbor_chunks", "-1"),
        ("context_neighbor_chunks", "4"),
        ("context_budget_tokens", "8000.0"),
        ("context_budget_tokens", '"8000"'),
        ("context_neighbor_chunks", "true"),
        # Exactly a whole number: no boolean, float or text, even one that spells a valid number.
        ("rrf_k", "true"),
        ("rrf_k", "false"),
        ("rrf_k", "60.0"),
        ("rrf_k", "6e1"),
        ("rrf_k", '"60"'),
        ("keyword_candidates", "true"),
        ("vector_candidates", "50.0"),
        ("max_chunks_per_document", '"3"'),
        ("rrf_k", "[60]"),
        ("rrf_k_typo", "60"),
    ],
)
def test_retrieval_values_must_be_whole_numbers_in_range(
    tmp_path: Path, key: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path / "c.toml", f"[retrieval]\n{key} = {value}\n"))


@pytest.mark.usefixtures("_no_retrieval_environment")
@pytest.mark.parametrize("value", [True, 60.0, "60", None, Decimal(60)])
def test_retrieval_values_in_code_are_exactly_whole_numbers(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"retrieval": {"rrf_k": value}})


@pytest.mark.usefixtures("_no_retrieval_environment")
def test_retrieval_values_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write(tmp_path / "c.toml", "[retrieval]\nrrf_k = 10\nkeyword_candidates = 20\n")
    monkeypatch.setenv("AIVAULT_RETRIEVAL__RRF_K", "30")
    monkeypatch.setenv("AIVAULT_RETRIEVAL__MAX_CHUNKS_PER_DOCUMENT", "0")
    with pytest.raises(ValidationError):  # in range from the environment too
        load_settings(config)
    monkeypatch.setenv("AIVAULT_RETRIEVAL__MAX_CHUNKS_PER_DOCUMENT", "5")
    retrieval = load_settings(config).retrieval
    assert (retrieval.rrf_k, retrieval.keyword_candidates, retrieval.max_chunks_per_document) == (
        30,
        20,
        5,
    )
    monkeypatch.setenv("AIVAULT_RETRIEVAL__RRF_K", "0")
    assert load_settings(config).retrieval.rrf_k == 0
    monkeypatch.setenv("AIVAULT_RETRIEVAL__CONTEXT_BUDGET_TOKENS", "120000")
    monkeypatch.setenv("AIVAULT_RETRIEVAL__CONTEXT_NEIGHBOR_CHUNKS", "0")
    retrieval = load_settings(config).retrieval
    assert (retrieval.context_budget_tokens, retrieval.context_neighbor_chunks) == (120000, 0)


@pytest.mark.usefixtures("_no_retrieval_environment")
@pytest.mark.parametrize(
    "text",
    [
        "030",
        "+30",
        "-0",
        " 30",
        "30 ",
        "30\n",
        "30\r",
        "30.0",
        "3e1",
        "true",
        "",
        "٣٠",
        "0x1e",
        "1_0",
    ],
)
def test_an_environment_value_must_be_plain_decimal(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setenv("AIVAULT_RETRIEVAL__RRF_K", text)
    with pytest.raises(ValidationError):
        load_settings()


@pytest.mark.usefixtures("_no_retrieval_environment")
def test_the_example_config_names_every_retrieval_setting_at_its_default() -> None:
    example = Path(__file__).resolve().parents[3] / "config.example.toml"
    with example.open("rb") as handle:
        written = tomllib.load(handle)["retrieval"]
    assert set(written) == set(RETRIEVAL_KEYS) == set(Settings().retrieval.model_dump())
    assert load_settings(example).retrieval == Settings().retrieval

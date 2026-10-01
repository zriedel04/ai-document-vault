"""Vault folder rules: outside every Git repository, created only after the path is accepted."""

from __future__ import annotations

from pathlib import Path

import pytest

from vault.core.config import Settings
from vault.main import create_app
from vault.storage.layout import VaultPathError, ensure_vault, validate_vault_root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A folder that is a Git working tree (the ``.git`` folder is enough for the check)."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def link(target: Path, name: Path) -> None:
    try:
        name.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:  # Windows without the privilege to create links
        pytest.skip("cannot create symbolic links here")


def test_a_new_vault_gets_its_folders(tmp_path: Path) -> None:
    paths = ensure_vault(tmp_path / "AIVault")
    for folder in (paths.root, paths.blobs, paths.derived, paths.logs, paths.backups):
        assert folder.is_dir()
    assert paths.db == paths.root / "vault.db"
    assert paths.index_db == paths.root / "index.db"
    assert paths.token_file == paths.root / "local-token"


def test_ensure_vault_keeps_what_is_already_there(tmp_path: Path) -> None:
    paths = ensure_vault(tmp_path / "AIVault")
    (paths.blobs / "keep.bin").write_bytes(b"data")
    assert ensure_vault(paths.root) == paths
    assert (paths.blobs / "keep.bin").read_bytes() == b"data"


def test_a_relative_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(VaultPathError, match="absolute"):
        ensure_vault(Path("vault"))
    assert not (tmp_path / "vault").exists()


def test_a_file_is_not_a_vault(tmp_path: Path) -> None:
    file = tmp_path / "vault"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(VaultPathError, match="not a folder"):
        ensure_vault(file)


@pytest.mark.parametrize(
    "inside",
    ["vault", "a/b/c/vault", "", "vault/../vault", ".git/vault", "backend/src/vault/x"],
)
def test_a_vault_inside_a_repository_is_refused_and_nothing_is_created(
    repo: Path, inside: str
) -> None:
    target = repo / inside
    before = sorted(repo.rglob("*"))
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(target)
    assert sorted(repo.rglob("*")) == before


def test_a_git_file_counts_too(tmp_path: Path) -> None:
    """A worktree or submodule has a ``.git`` file, not a folder."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(worktree / "vault")


def test_a_broken_git_link_counts_too(tmp_path: Path) -> None:
    folder = tmp_path / "odd"
    folder.mkdir()
    link(tmp_path / "does-not-exist", folder / ".git")
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(folder / "vault")


def test_a_bare_repository_counts_too(tmp_path: Path) -> None:
    bare = tmp_path / "project.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(bare / "vault")


def test_dotdot_cannot_climb_into_a_repository(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(outside / ".." / "repo" / "vault")


def test_a_link_into_a_repository_is_judged_by_where_it_points(tmp_path: Path, repo: Path) -> None:
    outside = tmp_path / "AIVault"
    link(repo / "data", outside)
    (repo / "data").mkdir()
    with pytest.raises(VaultPathError, match="inside a Git repository"):
        ensure_vault(outside)
    assert list((repo / "data").iterdir()) == []


def test_a_link_out_of_a_repository_is_allowed(tmp_path: Path, repo: Path) -> None:
    real = tmp_path / "AIVault"
    real.mkdir()
    # A path that starts in the repository but really lives outside it is fine to use.
    link(real, repo / "shortcut")
    assert ensure_vault(repo / "shortcut").root == real.resolve()


def test_neighbours_and_parents_of_a_repository_are_allowed(tmp_path: Path, repo: Path) -> None:
    assert ensure_vault(tmp_path / "AIVault").root.is_dir()
    assert ensure_vault(tmp_path / "repo-data").root.is_dir()  # a name that only starts like it
    assert validate_vault_root(tmp_path / "container") == (tmp_path / "container").resolve()
    # A folder that merely contains a repository is not inside one.
    assert validate_vault_root(repo.parent) == repo.parent.resolve()


def test_the_app_does_not_start_with_a_vault_inside_a_repository(repo: Path) -> None:
    settings = Settings.model_validate({"vault": {"path": str(repo / "vault")}})
    with pytest.raises(VaultPathError):
        create_app(settings)
    assert not (repo / "vault").exists()

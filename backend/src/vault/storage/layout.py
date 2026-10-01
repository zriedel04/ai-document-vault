"""The vault folder: where it may be, and the folders and files inside it.

The vault holds private documents, text and a database, so it must never sit inside a Git working
tree, where a stray ``git add .`` could commit it (``docs/architecture.md``, section 5).
:func:`ensure_vault` refuses such a path before it creates anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class VaultPathError(ValueError):
    """The configured vault folder cannot be used."""


@dataclass(frozen=True)
class VaultPaths:
    """Every location inside one vault folder."""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "vault.db"

    @property
    def index_db(self) -> Path:
        return self.root / "index.db"

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    @property
    def derived(self) -> Path:
        return self.root / "derived"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def token_file(self) -> Path:
        return self.root / "local-token"

    @property
    def folders(self) -> tuple[Path, ...]:
        return (self.root, self.blobs, self.derived, self.logs, self.backups)


def _is_git_dir(folder: Path) -> bool:
    """A bare repository has no ``.git`` entry, but has these three."""
    return (
        (folder / "HEAD").is_file() and (folder / "objects").is_dir() and (folder / "refs").is_dir()
    )


def _git_root_containing(path: Path) -> Path | None:
    """The nearest folder at or above ``path`` that is a Git working tree or repository."""
    for folder in (path, *path.parents):
        # ``.git`` is a folder, or a file in a worktree or submodule; ``lexists`` also counts a
        # symlink, even a broken one.
        if (folder / ".git").exists() or (folder / ".git").is_symlink() or _is_git_dir(folder):
            return folder
    return None


def validate_vault_root(path: Path) -> Path:
    """Return the resolved vault folder, or raise :class:`VaultPathError`. Creates nothing."""
    if not path.is_absolute():
        msg = "the vault path must be absolute (a relative path would depend on where you start)"
        raise VaultPathError(msg)
    # resolve() follows symlinks and ``..`` (and junctions on Windows) so a link into a repository,
    # or a path that climbs back into one, is judged by where it really points.
    root = path.resolve()
    if root.exists() and not root.is_dir():
        msg = "the vault path exists and is not a folder"
        raise VaultPathError(msg)
    if _git_root_containing(root) is not None:
        msg = (
            "the vault folder is inside a Git repository; choose a folder outside every "
            "repository, for example D:\\AIVault"
        )
        raise VaultPathError(msg)
    return root


def ensure_vault(path: Path) -> VaultPaths:
    """Validate ``path``, then create the vault folder and its subfolders."""
    paths = VaultPaths(validate_vault_root(path))
    for folder in paths.folders:
        folder.mkdir(parents=True, exist_ok=True)
    return paths

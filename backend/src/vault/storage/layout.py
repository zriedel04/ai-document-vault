"""The vault folder: where it may be, and the folders and files inside it.

The vault holds private documents, text and a database, so it must never sit inside a Git working
tree, where a stray ``git add .`` could commit it (``docs/architecture.md``, section 5).
:func:`ensure_vault` refuses such a path before it creates anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vault.storage import acl


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
    def trusted_entries(self) -> tuple[Path, ...]:
        """The files and folders the program reads at start-up and trusts to be private.

        SQLite keeps a database's recent pages in side files next to it: the write-ahead log and
        its shared-memory index (``-wal``, ``-shm``) and the rollback journal (``-journal``).
        Any of them can hold database pages, so each is as private as the database.
        """
        databases = (self.db, self.index_db)
        sidecars = tuple(
            Path(f"{db}{suffix}") for db in databases for suffix in ("-wal", "-shm", "-journal")
        )
        return (
            self.token_file,
            *databases,
            *sidecars,
            self.blobs,
            self.derived,
            self.logs,
            self.backups,
        )

    def entries_to_verify(self) -> list[Path]:
        """Every existing entry the start-up access check covers: the named ones above, and
        whatever else sits directly in the vault folder.

        A fixed list of names misses what SQLite or a person puts there under another name (a
        super-journal, a copied backup, a stray file), so the folder's own contents are listed
        too. These are checked first because the error can then give specific advice (for the
        token, delete it); everything deeper is checked by :func:`acl.verify_tree`.
        """
        entries = {p for p in self.trusted_entries if p.exists()}
        entries.update(self.root.iterdir())
        return sorted(entries)

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
            "repository, for example C:\\AIVault"
        )
        raise VaultPathError(msg)
    return root


def ensure_vault(path: Path) -> VaultPaths:
    """Validate ``path``, then create the vault folder and its subfolders.

    On Windows the vault folder is first made private to its owner (see :mod:`vault.storage.acl`),
    before the subfolders, the databases or the token are created inside it, so they inherit that.
    """
    paths = VaultPaths(validate_vault_root(path))
    created = not paths.root.exists()
    paths.root.mkdir(parents=True, exist_ok=True)
    if acl.IS_WINDOWS:
        try:
            acl.secure_vault_folder(paths.root, created=created)
        except acl.VaultAccessError as error:
            raise VaultPathError(str(error)) from error
    if acl.IS_WINDOWS:
        # Entries that were already there may carry their own access list (a file moved in
        # from another folder keeps it). New ones inherit the private folder's.
        try:
            acl.verify_private(paths.entries_to_verify())
            acl.verify_tree(paths.root)
        except acl.VaultAccessError as error:
            raise VaultPathError(str(error)) from error
    for folder in paths.folders:
        folder.mkdir(parents=True, exist_ok=True)
    return paths

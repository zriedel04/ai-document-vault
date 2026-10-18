"""Back up the vault: ``python -m vault.backup --to <folder>`` (``.\\vault.ps1 backup -To``).

The folder may also come in the ``VAULT_BACKUP_TO`` environment variable, which is how the launcher
passes it: a Windows PowerShell 5.1 command line mangles a quoted path that ends in a backslash.
The vault folder and the settings file are the server's (``vault.core.config``). What is copied and
how is in :mod:`vault.storage.backup`.

The output is counts and the name of the snapshot folder, which this package built from the time.
No file name, hash or folder is printed, the target included: messages are fixed text, an error
this package does not own is shown only by its class name, and a malformed command line is refused
without repeating what was typed (``argparse`` would quote it).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from vault.core.config import load_settings, resolve_config_file
from vault.storage.backup import BackupError, BackupResult, run_backup
from vault.storage.layout import VaultPathError, validate_vault_root

TARGET_ENV_VAR = "VAULT_BACKUP_TO"
EXIT_PROBLEMS = 1
EXIT_INTERRUPTED = 130


class FixedErrorParser(argparse.ArgumentParser):
    """An argument parser whose errors are fixed text (``argparse`` quotes the rejected token,
    which could be a private folder typed in the wrong place)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            "error: the command line is not valid; its arguments are listed above "
            "(the rejected text is not repeated because it could be a folder name)",
            file=sys.stderr,
        )
        raise SystemExit(2)


def parser() -> argparse.ArgumentParser:
    command = FixedErrorParser(
        prog="python -m vault.backup",
        description="Back up the vault's database and original files into a folder.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--to", metavar="FOLDER", help=f"the backup folder (default: ${TARGET_ENV_VAR})"
    )
    return command


def report(result: BackupResult) -> list[str]:
    """The lines printed after a backup: counts, and the snapshot name this package built."""
    lines = [
        f"Snapshot {result.snapshot} written.",
        f"Original files copied: {result.copied}",
        f"Original files already in the backup: {result.present}",
    ]
    if result.cleaned:
        lines.append(f"Leftovers of an interrupted backup tidied up: {result.cleaned}")
    if result.missing:
        lines.append(
            f"Files the snapshot lists that are not in the vault: {result.missing} (deleted "
            "since the snapshot was taken, or lost)"
        )
    if result.damaged:
        lines.append(f"Files that do not match their hash or record, not copied: {result.damaged}")
    if result.unreadable:
        lines.append(
            f"Files that could not be read, not copied: {result.unreadable} (another program "
            "may hold them; run the backup again)"
        )
    if result.mismatched:
        lines.append(
            f"Files already in the backup that do not match their record: {result.mismatched} "
            "(kept, not replaced)"
        )
    if result.incomplete:
        lines.append(
            f"Earlier snapshots that did not finish (a folder without vault.db): "
            f"{result.incomplete}; they can be deleted"
        )
    if result.token_missing:
        lines.append(
            "The vault has no local-token yet, so this snapshot has none (the server makes one "
            "when it starts)."
        )
    if result.private is False:
        lines.append(
            "Warning: other accounts on this PC may be able to read the backup folder, or its "
            "drive keeps no permissions. Keep backups on a BitLocker-encrypted drive, or keep "
            "only your account in the folder's Security tab."
        )
    return lines


def _backup(argv: Sequence[str] | None) -> BackupResult:
    args = parser().parse_args(argv)
    target = args.to or os.environ.get(TARGET_ENV_VAR)
    if not target:
        msg = "no backup folder was given; pass --to <folder>"
        raise BackupError(msg)
    try:
        settings = load_settings()
        config_file, _ = resolve_config_file()
    except Exception as error:
        # A settings error can quote the file's path or a value from it.
        msg = (
            f"the settings could not be read ({type(error).__name__}); check backend\\config.toml "
            "or the file AIVAULT_CONFIG names"
        )
        raise BackupError(msg) from None
    if settings.vault.path is None:
        msg = "no vault folder is set: set vault.path in backend\\config.toml"
        raise BackupError(msg)
    vault_root = validate_vault_root(settings.vault.path)
    return run_backup(
        vault_root,
        Path(target),
        config_file=config_file.absolute() if config_file.is_file() else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _backup(argv)
    except (BackupError, VaultPathError) as error:
        # Both carry fixed text only (vault.storage.backup and vault.storage.layout).
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: the backup was interrupted; nothing was overwritten", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:
        # An error from the operating system or a library quotes the file it failed on.
        print(
            f"error: the backup stopped ({type(error).__name__}); nothing was overwritten, and "
            "the next backup tidies up what this one left",
            file=sys.stderr,
        )
        return 1
    for line in report(result):
        print(line)
    if result.problems:
        print(
            "error: the backup finished, but some files were not copied (see the counts above)",
            file=sys.stderr,
        )
        return EXIT_PROBLEMS
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Restore a backup: ``python -m vault.restore --from <snapshot> --to <new vault folder>``
(``.\\vault.ps1 restore -From <snapshot> -To <folder>``).

``--from`` names one snapshot (``<backup folder>\\snapshots\\<time>``) or the backup folder, which
means its latest complete snapshot. ``--to`` is a vault folder that does not exist yet or is empty.
Both may also come in environment variables (``VAULT_RESTORE_FROM``, ``VAULT_RESTORE_TO``), which
is how the launcher passes them: a Windows PowerShell 5.1 command line mangles a quoted path that
ends in a backslash. The settings are read only to keep the target apart from the vault they name;
neither they nor the vault are changed. What is copied and how is in :mod:`vault.storage.restore`.

The output is counts and the name of the snapshot, a time. No file name, hash or folder is printed:
messages are fixed text, an error this package does not own is shown only by its class name, and a
malformed command line is refused without repeating what was typed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from vault.backup import FixedErrorParser
from vault.core.config import load_settings
from vault.storage.backup import BackupError
from vault.storage.layout import VaultPathError
from vault.storage.restore import RestoreError, RestoreResult, TokenState, run_restore

SOURCE_ENV_VAR = "VAULT_RESTORE_FROM"
TARGET_ENV_VAR = "VAULT_RESTORE_TO"
EXIT_PROBLEMS = 1
EXIT_INTERRUPTED = 130
TOKEN_LINES: dict[TokenState, str] = {
    "restored": "local-token restored: the token your browser keeps still works.",
    "absent": "The snapshot has no local-token; the server makes one when it starts.",
    "invalid": (
        "The snapshot's local-token is not a valid token and was left out; the server makes a "
        "new one when it starts."
    ),
}


def parser() -> FixedErrorParser:
    command = FixedErrorParser(
        prog="python -m vault.restore",
        description="Restore a backup snapshot into a new, empty vault folder.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--from",
        dest="source",
        metavar="FOLDER",
        help=f"a snapshot, or a backup folder for its latest snapshot (default: ${SOURCE_ENV_VAR})",
    )
    command.add_argument(
        "--to", metavar="FOLDER", help=f"the new vault folder (default: ${TARGET_ENV_VAR})"
    )
    return command


def report(result: RestoreResult) -> list[str]:
    """The lines printed after a restore: counts, and the snapshot name, which is a time."""
    lines = [
        f"Snapshot {result.snapshot} restored into the new vault folder.",
        f"Original files restored and checked against their hash: {result.restored}",
    ]
    if result.missing:
        lines.append(f"Files the snapshot lists that are not in the backup: {result.missing}")
    if result.damaged:
        lines.append(
            f"Files that do not match their hash or record, not restored: {result.damaged}"
        )
    if result.unreadable:
        lines.append(
            f"Files that could not be read, not restored: {result.unreadable} (another program "
            "may hold them)"
        )
    lines.append(TOKEN_LINES[result.token_state])
    if result.config:
        lines.append(
            "The snapshot's settings are in config.toml in the new vault folder, to compare: "
            "the app reads backend\\config.toml, and that copy's vault.path still names the "
            "old folder."
        )
    if result.leftovers:
        lines.append(
            f"Folders of restores that did not finish, beside the new vault folder (named "
            f".vault-restore-...): {result.leftovers}; delete them when no restore is running"
        )
    lines.append(
        "Next: set vault.path in backend\\config.toml to the new folder if it names another, "
        "start the app (it updates an older database), then run .\\vault.ps1 check."
    )
    return lines


def _restore(argv: Sequence[str] | None) -> RestoreResult:
    args = parser().parse_args(argv)
    source = args.source or os.environ.get(SOURCE_ENV_VAR)
    target = args.to or os.environ.get(TARGET_ENV_VAR)
    if not source:
        msg = "no snapshot was given; pass --from <snapshot folder or backup folder>"
        raise RestoreError(msg)
    if not target:
        msg = "no folder for the restored vault was given; pass --to <new folder>"
        raise RestoreError(msg)
    try:
        settings = load_settings()
    except Exception as error:
        # A settings error can quote the file's path or a value from it.
        msg = (
            f"the settings could not be read ({type(error).__name__}); check backend\\config.toml "
            "or the file AIVAULT_CONFIG names"
        )
        raise RestoreError(msg) from None
    vault = settings.vault.path
    return run_restore(
        Path(source),
        Path(target),
        vault_root=vault if vault is not None and vault.is_absolute() else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _restore(argv)
    except (BackupError, VaultPathError) as error:
        # Both carry fixed text only (vault.storage.backup, .restore and .layout).
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "error: the restore was interrupted; the target folder was left as it was",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    except Exception as error:
        # An error from the operating system or a library quotes the file it failed on.
        print(
            f"error: the restore stopped ({type(error).__name__}); the target folder was left "
            "as it was",
            file=sys.stderr,
        )
        return 1
    for line in report(result):
        print(line)
    if result.problems:
        print(
            "error: the restore finished, but some files could not be restored (see the counts "
            "above); .\\vault.ps1 check on the new vault lists them by hash",
            file=sys.stderr,
        )
        return EXIT_PROBLEMS
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

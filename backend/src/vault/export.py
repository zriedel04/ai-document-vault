"""Export the vault: ``python -m vault.export --to <folder>`` (``.\\vault.ps1 export -To``).

The folder may also come in the ``VAULT_EXPORT_TO`` environment variable, which is how the launcher
passes it, for the reason ``vault.backup`` gives. ``--include-trash`` adds the documents in the
trash, which are left out by default. What is written and how is in :mod:`vault.storage.export`.

The output is counts only. No file name, hash or folder is printed, the target included: messages
are fixed text, an error this package does not own is shown only by its class name, and a
malformed command line is refused without repeating what was typed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from vault.backup import FixedErrorParser
from vault.core.config import load_settings
from vault.storage.export import ExportError, ExportResult, run_export
from vault.storage.layout import VaultPathError, validate_vault_root

TARGET_ENV_VAR = "VAULT_EXPORT_TO"
EXIT_PROBLEMS = 1
EXIT_INTERRUPTED = 130


def parser() -> argparse.ArgumentParser:
    command = FixedErrorParser(
        prog="python -m vault.export",
        description="Export the vault's originals into a folder tree with a manifest.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--to", metavar="FOLDER", help=f"a new or empty folder (default: ${TARGET_ENV_VAR})"
    )
    command.add_argument(
        "--include-trash", action="store_true", help="also export the documents in the trash"
    )
    return command


def report(result: ExportResult) -> list[str]:
    """The lines printed after an export: counts only."""
    lines = [
        "Export written.",
        f"Documents exported: {result.exported}",
        f"Documents in the trash, left out: {result.trash_left_out}",
    ]
    if result.renamed:
        lines.append(
            f"Documents given a numbered name because another had theirs: {result.renamed}"
        )
    if result.placed_higher:
        lines.append(
            f"Documents placed above their folder to keep paths short: {result.placed_higher}"
        )
    if result.missing:
        lines.append(f"Documents whose original is not in the vault: {result.missing}")
    if result.damaged:
        lines.append(
            f"Documents whose original does not match its hash or record: {result.damaged}"
        )
    if result.unreadable:
        lines.append(
            f"Documents whose original could not be read: {result.unreadable} (another program "
            "may hold it)"
        )
    if result.leftovers:
        lines.append(
            f"Folders of earlier exports that did not finish (.vault-export-...) beside this one: "
            f"{result.leftovers}; they can be deleted"
        )
    if result.private is False:
        lines.append(
            "Warning: other accounts on this PC may be able to read the export, or its drive "
            "keeps no permissions. Keep only your account in the folder's Security tab."
        )
    return lines


def _export(argv: Sequence[str] | None) -> ExportResult:
    args = parser().parse_args(argv)
    target = args.to or os.environ.get(TARGET_ENV_VAR)
    if not target:
        msg = "no export folder was given; pass --to <folder>"
        raise ExportError(msg)
    try:
        settings = load_settings()
    except Exception as error:
        # A settings error can quote the file's path or a value from it.
        msg = (
            f"the settings could not be read ({type(error).__name__}); check backend\\config.toml "
            "or the file AIVAULT_CONFIG names"
        )
        raise ExportError(msg) from None
    if settings.vault.path is None:
        msg = "no vault folder is set: set vault.path in backend\\config.toml"
        raise ExportError(msg)
    vault_root = validate_vault_root(settings.vault.path)
    return run_export(vault_root, Path(target), include_trash=args.include_trash)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = _export(argv)
    except (ExportError, VaultPathError) as error:
        # Both carry fixed text only (vault.storage.export and vault.storage.layout).
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: the export was interrupted; nothing was left under its name", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:
        # An error from the operating system or a library quotes the file it failed on.
        print(
            f"error: the export stopped ({type(error).__name__}); nothing was left under its name",
            file=sys.stderr,
        )
        return 1
    for line in report(result):
        print(line)
    if result.problems:
        print(
            "error: the export finished, but some originals were not written (see the counts "
            "above; manifest.json says which)",
            file=sys.stderr,
        )
        return EXIT_PROBLEMS
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

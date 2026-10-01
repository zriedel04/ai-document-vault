#!/usr/bin/env python3
"""Keep private data, secrets and oversized files out of the repository.

AI Document Vault manages personal documents, so this repository must never
contain real documents or anything derived from them. This check is one of
three layers, together with .gitignore and gitleaks. It runs:

* as a pre-commit hook, on the staged version of the files being committed, and
* in CI, on every tracked file and (with --history) on every file version that
  was ever committed, whatever it was called at the time.

It uses only the Python standard library, so it works before any project
tooling is installed.

Usage:
    python scripts/check_repo_safety.py              # all tracked files, as staged in the index
    python scripts/check_repo_safety.py --history    # ...plus the full git history
    python scripts/check_repo_safety.py PATH [PATH ...]            # files on disk
    python scripts/check_repo_safety.py --staged PATH [PATH ...]   # staged version of each file
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MIB = 1024 * 1024
MAX_FILE_BYTES = 1 * MIB
"""Size limit for files outside the fixture folders."""
MAX_LARGE_FILE_BYTES = 5 * MIB
"""Size limit for synthetic fixtures and dependency lock files."""

LARGE_FILE_NAMES = frozenset({"package-lock.json", "uv.lock"})
SQLITE_HEADER = b"SQLite format 3\x00"

DATABASE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
KEY_SUFFIXES = (
    ".gpg",
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".p8",
    ".pem",
    ".pfx",
    ".pgp",
    ".ppk",
)
SSH_KEY_PREFIXES = ("id_dsa", "id_ecdsa", "id_ed25519", "id_rsa")
MODEL_SUFFIXES = (".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors")
DOCUMENT_SUFFIXES = (
    ".azw",
    ".azw3",
    ".csv",
    ".djvu",
    ".doc",
    ".docm",
    ".docx",
    ".dotx",
    ".epub",
    ".eml",
    ".mobi",
    ".msg",
    ".numbers",
    ".odp",
    ".ods",
    ".odt",
    ".pages",
    ".pdf",
    ".potx",
    ".ppt",
    ".pptx",
    ".rtf",
    ".tsv",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xps",
)
IMAGE_SUFFIXES = (".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
PRIVATE_DIR_NAMES = frozenset(
    {".cache", "backups", "blobs", "derived", "exports", "logs", "secrets"}
)
"""Folders that hold private data wherever they appear in the tree."""
ROOT_PRIVATE_DIR_NAMES = frozenset({"aivault", "models", "vault", "vaults"})
"""Private only at the repository root: backend/src/vault/ is source code."""
IMAGE_DIR_PREFIXES = ("docs/images/", "frontend/public/", "frontend/src/assets/")


@dataclass(frozen=True)
class Violation:
    path: str
    reason: str
    source: str = "current"


def _normalise(path: str) -> PurePosixPath:
    """Repository-relative POSIX path, lower-cased (Windows paths are case-insensitive)."""
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return PurePosixPath(cleaned.lower())


def is_fixture_path(path: str) -> bool:
    """Synthetic test data lives under demo/corpus/ or any tests/**/fixtures/ folder."""
    parts = _normalise(path).parts
    if parts[:2] == ("demo", "corpus"):
        return True
    return any(
        part == "fixtures" and "tests" in parts[:index] for index, part in enumerate(parts[:-1])
    )


def path_problems(path: str) -> list[str]:
    """Reasons a file must not be committed, judged by its path alone."""
    normalised = _normalise(path)
    name = normalised.name
    folders = normalised.parts[:-1]
    fixture = is_fixture_path(path)
    problems: list[str] = []

    if name.endswith(DATABASE_SUFFIXES):
        problems.append("database file")
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        problems.append("environment file (only .env.example may be committed)")
    if name == "config.toml" or name.endswith(".local.toml"):
        problems.append(
            "local configuration (only config.example.toml and config.demo.toml may be committed)"
        )
    if name.endswith(KEY_SUFFIXES) or (
        name.startswith(SSH_KEY_PREFIXES) and not name.endswith(".pub")
    ):
        problems.append("private key or certificate")
    if name.endswith(MODEL_SUFFIXES):
        problems.append("model weights (downloaded at install time, never committed)")
    if name.endswith(".log"):
        problems.append("log file")
    private_folders = sorted(PRIVATE_DIR_NAMES.intersection(folders))
    if folders[:1] and folders[0] in ROOT_PRIVATE_DIR_NAMES:
        private_folders.append(folders[0])
    if private_folders:
        problems.append(f"inside a private data folder ({private_folders[0]}/)")
    if folders[:2] == ("eval", "private"):
        problems.append("private evaluation data")
    if name.endswith(DOCUMENT_SUFFIXES) and not fixture:
        problems.append(
            "document outside the synthetic fixture folders (tests/**/fixtures/, demo/corpus/)"
        )
    if (
        name.endswith(IMAGE_SUFFIXES)
        and not fixture
        and not normalised.as_posix().startswith(IMAGE_DIR_PREFIXES)
    ):
        problems.append("image outside the fixture, docs/images/ and frontend asset folders")
    return problems


def size_problem(path: str, size: int) -> str | None:
    """A reason if the file is too large for where it lives."""
    large_allowed = is_fixture_path(path) or _normalise(path).name in LARGE_FILE_NAMES
    limit = MAX_LARGE_FILE_BYTES if large_allowed else MAX_FILE_BYTES
    if size > limit:
        return f"{size / MIB:.1f} MiB is over the {limit / MIB:.0f} MiB limit"
    return None


def content_problem(head: bytes) -> str | None:
    """A reason if the first bytes show a database, whatever the file is called."""
    if head.startswith(SQLITE_HEADER):
        return "SQLite database detected from file content"
    return None


def reasons_for(path: str, size: int | None, head: bytes) -> list[str]:
    """Every reason a file must not be committed, from its path, size and first bytes.

    ``size`` is None when the content is unavailable, so only the path is judged.
    """
    reasons = path_problems(path)
    if size is None:
        return reasons
    problem = size_problem(path, size)
    if problem:
        reasons.append(problem)
    if not any(reason.startswith("database") for reason in reasons):
        problem = content_problem(head)
        if problem:
            reasons.append(problem)
    return reasons


def check_files(entries: Iterable[tuple[str, Path]]) -> list[Violation]:
    """Check (repository-relative path, file on disk) pairs."""
    violations: list[Violation] = []
    for relative, on_disk in entries:
        size: int | None = None
        head = b""
        if on_disk.is_file():
            size = on_disk.stat().st_size
            with on_disk.open("rb") as handle:
                head = handle.read(len(SQLITE_HEADER))
        reasons = reasons_for(relative, size, head)
        violations.extend(Violation(relative, reason) for reason in reasons)
    return violations


def blob_facts(root: Path, object_ids: Iterable[str]) -> dict[str, tuple[int, bytes]]:
    """Size and first bytes of each blob. Objects that are missing or not blobs are left out."""
    ids = sorted(set(object_ids))
    if not ids:
        return {}
    listing = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_text="\n".join(ids) + "\n",
    )
    sizes: dict[str, int] = {}
    for line in listing.splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    # Over-size blobs are rejected by size alone, so their content is never read.
    readable = [oid for oid in ids if 0 < sizes.get(oid, 0) <= MAX_LARGE_FILE_BYTES]
    heads = _read_heads(root, readable)
    return {oid: (size, heads.get(oid, b"")) for oid, size in sizes.items()}


def _read_heads(root: Path, object_ids: Sequence[str]) -> dict[str, bytes]:
    """First bytes of each blob, streamed so no blob is held in memory."""
    heads: dict[str, bytes] = {}
    if not object_ids:
        return heads
    with tempfile.TemporaryFile() as feed:
        feed.write(("\n".join(object_ids) + "\n").encode())
        feed.seek(0)
        with subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root, stdin=feed, stdout=subprocess.PIPE
        ) as process:
            assert process.stdout is not None
            for object_id in object_ids:
                size = int(process.stdout.readline().split()[2])
                head = process.stdout.read(min(size, len(SQLITE_HEADER)))
                heads[object_id] = head
                remaining = size - len(head) + 1  # the newline after the content
                while remaining > 0:
                    chunk = process.stdout.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
    return heads


def index_entries(root: Path, paths: Sequence[str] = ()) -> dict[str, str]:
    """Map path to blob id for files in the git index (all of them, or just ``paths``)."""
    args = ["--literal-pathspecs", "ls-files", "--stage", "-z"]
    if paths:
        args += ["--", *paths]
    entries: dict[str, str] = {}
    for record in _git(root, *args).split("\0"):
        meta, _, path = record.partition("\t")
        if not path:
            continue
        mode, object_id, _stage = meta.split(" ")
        if mode != "160000":  # a submodule is a commit, not a file
            entries[path] = object_id
    return entries


def check_index(root: Path, paths: Sequence[str] = ()) -> tuple[list[Violation], set[str]]:
    """Check files as staged in the index, which is what a commit would contain.

    Returns the violations and the paths that were found in the index.
    """
    entries = index_entries(root, paths)
    facts = blob_facts(root, entries.values())
    violations: list[Violation] = []
    for path, object_id in entries.items():
        size, head = facts.get(object_id, (None, b""))
        violations.extend(Violation(path, reason) for reason in reasons_for(path, size, head))
    return violations, set(entries)


def history_files(root: Path) -> set[tuple[str, str]]:
    """Every (blob id, path) pair that any commit on any ref ever contained.

    Walks the changes of every commit, so a file renamed or copied under another name
    is still seen under each of its names. ``rev-list --objects`` would report a blob
    only once, under one name.
    """
    log = _git(
        root,
        "log",
        "--all",
        "--no-renames",
        "-m",
        "--raw",
        "--no-abbrev",
        "-z",
        "--format=",
    )
    tokens = log.split("\0")
    pairs: set[tuple[str, str]] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith(":") or index + 1 >= len(tokens):
            index += 1
            continue
        _old_mode, new_mode, _old_id, new_id, status = token[1:].split(" ")
        if status != "D" and new_mode != "160000":
            pairs.add((new_id, tokens[index + 1]))
        index += 2
    return pairs


def check_history(root: Path) -> list[Violation]:
    """Check every file version ever committed: its name, size and first bytes."""
    pairs = history_files(root)
    facts = blob_facts(root, (object_id for object_id, _ in pairs))
    found: set[Violation] = set()
    for object_id, path in pairs:
        size, head = facts.get(object_id, (None, b""))
        found.update(Violation(path, reason, "history") for reason in reasons_for(path, size, head))
    return sorted(found, key=lambda violation: (violation.path, violation.reason))


def _git(cwd: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    return result.stdout


def _repo_root(start: Path) -> Path | None:
    try:
        return Path(_git(start, "rev-parse", "--show-toplevel").strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _explicit_entries(paths: Sequence[str], root: Path) -> list[tuple[str, Path]]:
    entries = []
    for path in paths:
        on_disk = (Path.cwd() / path).resolve()
        try:
            relative = on_disk.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = path
        entries.append((relative, on_disk))
    return entries


def _report(violations: Sequence[Violation]) -> None:
    print("Repository safety check failed. These files must not be committed:", file=sys.stderr)
    for source in ("current", "history"):
        reasons_by_path: dict[str, list[str]] = {}
        for violation in violations:
            if violation.source == source:
                reasons_by_path.setdefault(violation.path, []).append(violation.reason)
        if not reasons_by_path:
            continue
        if source == "history":
            print(
                "\nAlready in git history (deleting the file now does not remove it from history):",
                file=sys.stderr,
            )
        for path in sorted(reasons_by_path):
            print(f"  {path}", file=sys.stderr)
            for reason in reasons_by_path[path]:
                print(f"      - {reason}", file=sys.stderr)
    print(
        "\nSynthetic test files belong in tests/**/fixtures/ or demo/corpus/."
        " Never weaken this check to let real documents or data in; see CONTRIBUTING.md.",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")

    parser = argparse.ArgumentParser(
        description="Block private data, secrets and oversized files from the repository."
    )
    parser.add_argument("paths", nargs="*", help="files to check (default: all tracked files)")
    parser.add_argument(
        "--history", action="store_true", help="also check every file in the git history"
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check the staged version of each given file, not the copy on disk",
    )
    args = parser.parse_args(argv)

    root = _repo_root(Path.cwd())
    if root is None and (args.history or args.staged or not args.paths):
        print("Not inside a git repository; pass file paths to check.", file=sys.stderr)
        return 2

    if not args.paths:
        assert root is not None
        violations, checked = check_index(root)
        count = len(checked)
    else:
        entries = _explicit_entries(args.paths, root or Path.cwd())
        violations = []
        staged: set[str] = set()
        if args.staged:
            assert root is not None
            violations, staged = check_index(root, [relative for relative, _ in entries])
        violations.extend(check_files(entry for entry in entries if entry[0] not in staged))
        count = len(entries)

    if args.history:
        assert root is not None
        already_reported = {(violation.path, violation.reason) for violation in violations}
        violations.extend(
            violation
            for violation in check_history(root)
            if (violation.path, violation.reason) not in already_reported
        )

    if violations:
        _report(violations)
        return 1
    scope = " and git history" if args.history else ""
    print(f"Repository safety check passed: {count} files{scope} checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

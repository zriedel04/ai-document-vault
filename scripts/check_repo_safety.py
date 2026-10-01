#!/usr/bin/env python3
"""Keep private data, secrets and oversized files out of the repository.

AI Document Vault manages personal documents, so this repository must never
contain real documents or anything derived from them. This check is one of
three layers, together with .gitignore and gitleaks. It runs:

* as a pre-commit hook, on the files being committed, and
* in CI, on every tracked file and (with --history) on every file that was
  ever committed.

It uses only the Python standard library, so it works before any project
tooling is installed.

Usage:
    python scripts/check_repo_safety.py              # all tracked files
    python scripts/check_repo_safety.py --history    # ...plus the full git history
    python scripts/check_repo_safety.py PATH [PATH ...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
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
KEY_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
SSH_KEY_PREFIXES = ("id_dsa", "id_ecdsa", "id_ed25519", "id_rsa")
MODEL_SUFFIXES = (".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors")
DOCUMENT_SUFFIXES = (
    ".csv",
    ".doc",
    ".docx",
    ".eml",
    ".msg",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
)
IMAGE_SUFFIXES = (".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
PRIVATE_DIR_NAMES = frozenset({"backups", "blobs", "derived", "exports", "logs"})
IMAGE_DIR_PREFIXES = ("docs/images/", "frontend/public/", "frontend/src/assets/")


@dataclass(frozen=True)
class Violation:
    path: str
    reason: str
    source: str = "working tree"


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


def check_files(entries: Iterable[tuple[str, Path]]) -> list[Violation]:
    """Check (repository-relative path, file on disk) pairs."""
    violations: list[Violation] = []
    for relative, on_disk in entries:
        reasons = path_problems(relative)
        if on_disk.is_file():
            problem = size_problem(relative, on_disk.stat().st_size)
            if problem:
                reasons.append(problem)
            if not any(reason.startswith("database") for reason in reasons):
                with on_disk.open("rb") as handle:
                    problem = content_problem(handle.read(len(SQLITE_HEADER)))
                if problem:
                    reasons.append(problem)
        violations.extend(Violation(relative, reason) for reason in reasons)
    return violations


def history_blobs(root: Path) -> Iterator[tuple[str, int]]:
    """Yield (path, size) for every file version reachable from any ref."""
    paths_by_object: dict[str, set[str]] = {}
    for line in _git(root, "rev-list", "--objects", "--all").splitlines():
        object_id, _, path = line.partition(" ")
        if path:
            paths_by_object.setdefault(object_id, set()).add(path)
    if not paths_by_object:
        return
    listing = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_text="\n".join(paths_by_object) + "\n",
    )
    for line in listing.splitlines():
        object_id, object_type, size = line.split(" ")
        if object_type == "blob":
            for path in sorted(paths_by_object[object_id]):
                yield path, int(size)


def check_history(root: Path) -> list[Violation]:
    """Check the name and size of every file ever committed."""
    found: set[Violation] = set()
    for path, size in history_blobs(root):
        reasons = path_problems(path)
        problem = size_problem(path, size)
        if problem:
            reasons.append(problem)
        found.update(Violation(path, reason, "history") for reason in reasons)
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
    for source in ("working tree", "history"):
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
    args = parser.parse_args(argv)

    root = _repo_root(Path.cwd())
    if root is None and (args.history or not args.paths):
        print("Not inside a git repository; pass file paths to check.", file=sys.stderr)
        return 2

    if args.paths:
        entries = _explicit_entries(args.paths, root or Path.cwd())
    else:
        assert root is not None
        tracked = [path for path in _git(root, "ls-files", "-z").split("\0") if path]
        entries = [(path, root / path) for path in tracked]

    violations = check_files(entries)
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
    print(f"Repository safety check passed: {len(entries)} files{scope} checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

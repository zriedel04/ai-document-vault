"""The backup (``vault.storage.backup``) and its command line (``python -m vault.backup``).

Every vault and target is a temporary folder with synthetic documents.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from vault import backup as cli
from vault.core import security
from vault.core.security import load_or_create_token
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.ingest.importer import Importer, ImportRequest
from vault.storage import acl
from vault.storage import backup as backup_module
from vault.storage.backup import (
    BackupError,
    BackupResult,
    check_target,
    run_backup,
)
from vault.storage.blobs import (
    BlobMismatchError,
    BlobStore,
)
from vault.storage.blobs import _lstat as blobs_lstat
from vault.storage.blobs import publish_new as publish_new_blob

DOCUMENTS = {
    "syllabus.txt": b"Course syllabus\nWeek 1: introduction\nWeek 2: sources\n",
    "notes.md": b"# Lab notes\n\nThe sample was weighed twice.\n",
    "plan.txt": b"Study plan\nMonday: reading\nTuesday: problem set\n",
}
PRIVATE_NAME = "Patient-Diagnosis-Confidential"
START = datetime(2026, 10, 17, 14, 30, 5, tzinfo=UTC)
CONFIG_TEXT = "# synthetic settings for a test\n[server]\nport = 8765\n"
TOKEN_TEXT = "SyntheticTokenForTests_0123456789abcdefghij"  # noqa: S105 - synthetic, 43 characters


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clock(start: datetime = START) -> Callable[[], datetime]:
    """A clock that moves on a second at each reading, so each run gets its own snapshot."""
    state = {"now": start - timedelta(seconds=1)}

    def now() -> datetime:
        state["now"] += timedelta(seconds=1)
        return state["now"]

    return now


def is_read_only(path: Path) -> bool:
    return stat.S_IMODE(os.lstat(path).st_mode) & 0o222 == 0


def link(target: Path, name: Path) -> Path:
    name.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, name, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make symbolic links here")
    return name


def files_below(folder: Path) -> set[str]:
    return {
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() or path.is_symlink()
    }


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault folder with a migrated database and three imported synthetic documents, plus the
    rebuildable and secret files a backup must leave out."""
    root = tmp_path / "vault"
    root.mkdir()
    upgrade_to_latest(root / "vault.db")
    engine = create_vault_engine(root / "vault.db")
    try:
        store = BlobStore(root / "blobs")
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        for name, data in DOCUMENTS.items():
            (incoming / name).write_bytes(data)
            outcome = Importer(engine, store).import_file(
                ImportRequest(
                    path=incoming / name,
                    original_filename=name,
                    method="upload",
                    batch_id=ids.new_ulid(),
                )
            )
            assert outcome.reason is None
    finally:
        engine.dispose()
    (root / "index.db").write_bytes(b"rebuildable embeddings")
    (root / "derived" / "01ABC").mkdir(parents=True)
    (root / "derived" / "01ABC" / "page-1.json").write_text("{}")
    (root / "logs").mkdir()
    (root / "logs" / "vault.log").write_text("log line\n")
    (root / "local-token").write_text(TOKEN_TEXT)
    (root / "local-token").chmod(0o600)
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "backups" / PRIVATE_NAME


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TEXT)
    return path


def backup(vault: Path, target: Path, **kwargs: object) -> BackupResult:
    kwargs.setdefault("now", clock())
    return run_backup(vault, target, **kwargs)  # type: ignore[arg-type]


class TestABackup:
    def test_it_writes_a_dated_snapshot_and_every_original(
        self, vault: Path, target: Path, config_file: Path
    ) -> None:
        result = backup(vault, target, config_file=config_file)
        assert result.snapshot == "2026-10-17T143005Z"
        assert (result.copied, result.present, result.problems) == (3, 0, 0)
        snapshot = target / "snapshots" / result.snapshot
        assert sorted(p.name for p in snapshot.iterdir()) == [
            "config.toml",
            "local-token",
            "vault.db",
        ]
        assert (snapshot / "config.toml").read_text() == CONFIG_TEXT
        store = BlobStore(target / "blobs")
        for data in DOCUMENTS.values():
            found = store.find(sha(data))
            assert found is not None
            assert found.read_bytes() == data
            assert is_read_only(found)
        assert is_read_only(snapshot / "vault.db")
        assert is_read_only(snapshot / "config.toml")

    def test_the_snapshot_is_a_complete_database(self, vault: Path, target: Path) -> None:
        result = backup(vault, target)
        copy = sqlite3.connect(target / "snapshots" / result.snapshot / "vault.db")
        try:
            assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            hashes = {row[0] for row in copy.execute("SELECT sha256 FROM blobs")}
            assert copy.execute("SELECT count(*) FROM documents").fetchone() == (3,)
        finally:
            copy.close()
        assert hashes == {sha(data) for data in DOCUMENTS.values()}

    def test_rebuildable_data_and_logs_are_left_out_and_the_token_is_kept(
        self, vault: Path, target: Path
    ) -> None:
        result = backup(vault, target)
        written = files_below(target)
        blobs = {
            f"blobs/sha256/{h[:2]}/{h[2:4]}/{h}.{name.rsplit('.', 1)[1]}"
            for name, data in DOCUMENTS.items()
            for h in [sha(data)]
        }
        snapshot = f"snapshots/{result.snapshot}"
        assert written == {"backup.lock", f"{snapshot}/vault.db", f"{snapshot}/local-token", *blobs}
        assert not result.token_missing

    def test_the_token_is_copied_owner_only_as_adr_0016_says(
        self, vault: Path, target: Path
    ) -> None:
        result = backup(vault, target)
        copy = target / "snapshots" / result.snapshot / "local-token"
        assert copy.read_text() == TOKEN_TEXT
        assert is_read_only(copy)
        if os.name != "nt":  # Windows has only the read-only attribute; its list is checked below
            assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o400
        assert list((target / "snapshots" / result.snapshot).glob("*.partial")) == []

    def test_a_vault_without_a_token_is_backed_up_without_one(
        self, vault: Path, target: Path
    ) -> None:
        (vault / "local-token").unlink()
        result = backup(vault, target)
        assert result.token_missing
        assert result.problems == 0  # the server makes one at its first start
        assert not (target / "snapshots" / result.snapshot / "local-token").exists()
        assert (target / "snapshots" / result.snapshot / "vault.db").is_file()
        assert "no local-token yet" in "\n".join(cli.report(result))

    def test_a_link_in_place_of_the_token_is_refused(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside-token"
        outside.write_text("not the vault's token")
        (vault / "local-token").unlink()
        link(outside, vault / "local-token")
        with pytest.raises(BackupError, match="token file"):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []

    def test_a_folder_in_place_of_the_token_is_refused(self, vault: Path, target: Path) -> None:
        (vault / "local-token").unlink()
        (vault / "local-token").mkdir()
        with pytest.raises(BackupError, match="token file"):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []

    @pytest.mark.parametrize(
        ("content", "valid"),
        [
            pytest.param(b"", False, id="empty"),
            pytest.param(TOKEN_TEXT[:20].encode(), False, id="truncated"),
            pytest.param(("é" * 40).encode(), False, id="not-ascii"),
            pytest.param(TOKEN_TEXT.encode() + b"\xff", False, id="a-stray-byte"),
            pytest.param(TOKEN_TEXT.encode() + b"\n", True, id="trailing-newline"),
            pytest.param(TOKEN_TEXT.encode() + b"\r\n", True, id="windows-line-end"),
            pytest.param(TOKEN_TEXT.encode(), True, id="valid"),
        ],
    )
    def test_the_backup_judges_the_token_as_start_up_does(
        self, vault: Path, target: Path, content: bytes, valid: bool
    ) -> None:
        """Only a token that start-up would accept is kept: a snapshot with any other would give
        a restored vault that cannot start (Codex P2 on PR #59)."""
        (vault / "local-token").write_bytes(content)
        try:
            load_or_create_token(vault / "local-token")
        except ValueError:
            starts = False
        else:
            starts = True
        assert starts is valid  # what start-up decides, independently of the backup
        if valid:
            result = backup(vault, target)
            copy = target / "snapshots" / result.snapshot / "local-token"
            assert copy.read_bytes() == content  # copied as it is, line end included
        else:
            with pytest.raises(BackupError, match="does not hold a valid token"):
                backup(vault, target)
            assert list((target / "snapshots").iterdir()) == []

    def test_the_backup_and_start_up_share_one_token_rule(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bytes] = []

        def refuse_all(data: bytes) -> str | None:
            seen.append(data)
            return None

        monkeypatch.setattr(security, "parse_token", refuse_all)
        with pytest.raises(ValueError, match="not a valid token"):
            load_or_create_token(vault / "local-token")
        with pytest.raises(BackupError, match="does not hold a valid token"):
            backup(vault, target)
        assert seen == [TOKEN_TEXT.encode(), TOKEN_TEXT.encode()]  # start-up, then the backup

    def test_a_token_file_far_larger_than_a_token_is_refused(
        self, vault: Path, target: Path
    ) -> None:
        (vault / "local-token").write_bytes(b"x" * 5000)
        with pytest.raises(BackupError, match="larger than a token"):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []

    def test_a_token_file_that_cannot_be_opened_stops_the_run(
        self, vault: Path, target: Path
    ) -> None:
        real_open = os.open

        def refuse(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if str(path).endswith("local-token") and str(vault) in str(path):
                raise PermissionError(13, "held by another program")
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(os, "open", refuse), pytest.raises(BackupError, match="token"):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []

    def test_a_token_file_swapped_after_the_check_is_refused(
        self, vault: Path, target: Path
    ) -> None:
        real_lstat = blobs_lstat

        def stale(path: Path) -> os.stat_result | None:
            status = real_lstat(path)
            if path.name == "local-token" and status is not None:
                values = list(status)
                values[stat.ST_INO] += 1  # as if another file had taken the name since
                # The sequence holds only the first ten fields. The named ones (st_file_attributes
                # on Windows, the float times) come from the mapping or would come back as None.
                named = {n: getattr(status, n) for n in dir(status) if n.startswith("st_")}
                return os.stat_result(values, named)
            return status

        with (
            mock.patch("vault.storage.backup._lstat", stale),
            pytest.raises(BackupError, match="changed while"),
        ):
            backup(vault, target)

    def test_a_second_run_copies_nothing_new(self, vault: Path, target: Path) -> None:
        now = clock()
        first = run_backup(vault, target, now=now)
        before = {p: os.lstat(p).st_mtime_ns for p in (target / "blobs").rglob("*") if p.is_file()}
        second = run_backup(vault, target, now=now)
        assert (second.copied, second.present, second.problems) == (0, 3, 0)
        assert second.snapshot != first.snapshot
        after = {p: os.lstat(p).st_mtime_ns for p in (target / "blobs").rglob("*") if p.is_file()}
        assert after == before
        assert (target / "snapshots" / first.snapshot / "vault.db").is_file()
        assert (target / "snapshots" / second.snapshot / "vault.db").is_file()

    def test_a_new_document_is_the_only_copy_in_the_next_run(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        now = clock()
        run_backup(vault, target, now=now)
        engine = create_vault_engine(vault / "vault.db")
        try:
            source = tmp_path / "incoming" / "late.txt"
            source.write_bytes(b"A document added after the first backup\n")
            Importer(engine, BlobStore(vault / "blobs")).import_file(
                ImportRequest(
                    path=source, original_filename="late.txt", method="upload",
                    batch_id=ids.new_ulid(),
                )
            )  # fmt: skip
        finally:
            engine.dispose()
        result = run_backup(vault, target, now=now)
        assert (result.copied, result.present) == (1, 3)

    def test_an_existing_snapshot_is_never_overwritten(self, vault: Path, target: Path) -> None:
        fixed = lambda: START  # noqa: E731
        first = run_backup(vault, target, now=fixed)
        database = target / "snapshots" / first.snapshot / "vault.db"
        before = database.read_bytes()
        with pytest.raises(BackupError, match="already in the target"):
            run_backup(vault, target, now=fixed)
        assert database.read_bytes() == before

    def test_a_blob_is_published_only_once_it_matches_its_hash(
        self, vault: Path, target: Path
    ) -> None:
        data = DOCUMENTS["notes.md"]
        stored = BlobStore(vault / "blobs").find(sha(data))
        assert stored is not None
        stored.chmod(0o644)
        stored.write_bytes(data.upper())  # same size, other bytes: damage in the vault
        result = backup(vault, target)
        assert (result.copied, result.damaged) == (2, 1)
        target_store = BlobStore(target / "blobs")
        assert target_store.find(sha(data)) is None
        assert target_store.find(sha(data.upper())) is None
        assert list((target / "blobs" / ".tmp").iterdir()) == []

    def test_a_blob_larger_than_its_record_is_not_copied(self, vault: Path, target: Path) -> None:
        data = DOCUMENTS["plan.txt"]
        stored = BlobStore(vault / "blobs").find(sha(data))
        assert stored is not None
        stored.chmod(0o644)
        stored.write_bytes(data + b"more\n")
        result = backup(vault, target)
        assert result.damaged == 1
        assert BlobStore(target / "blobs").find(sha(data)) is None

    def test_a_blob_shorter_than_its_record_is_not_copied(self, vault: Path, target: Path) -> None:
        """The bytes match the hash, but the record says more bytes: ``max_bytes`` alone is only a
        ceiling, so the size has to be exact (Codex P2 on PR #59)."""
        data = DOCUMENTS["plan.txt"]
        real = backup_module._snapshot

        def snapshot(db: Path, out: Path) -> list[tuple[object, object, object]]:
            rows = real(db, out)
            return [
                (h, ext, size + 5 if h == sha(data) else size)  # type: ignore[operator]
                for h, ext, size in rows
            ]

        with mock.patch.object(backup_module, "_snapshot", snapshot):
            result = backup(vault, target)
        assert (result.copied, result.damaged) == (2, 1)
        assert BlobStore(target / "blobs").find(sha(data)) is None
        assert list((target / "blobs" / ".tmp").iterdir()) == []

    def test_a_blob_missing_from_the_vault_is_counted(self, vault: Path, target: Path) -> None:
        assert BlobStore(vault / "blobs").delete(sha(DOCUMENTS["syllabus.txt"]))
        result = backup(vault, target)
        assert (result.copied, result.missing) == (2, 1)
        # The snapshot is still published: it is consistent, and the count says what is missing.
        assert (target / "snapshots" / result.snapshot / "vault.db").is_file()

    def test_a_blob_that_cannot_be_read_is_counted_and_the_rest_go_on(
        self, vault: Path, target: Path
    ) -> None:
        real_open = Path.open
        unreadable = sha(DOCUMENTS["notes.md"])

        def failing_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
            if self.name.startswith(unreadable) and mode == "rb" and str(vault) in str(self):
                raise PermissionError(13, "held by another program")
            return real_open(self, mode, *args, **kwargs)  # type: ignore[call-overload]

        with mock.patch.object(Path, "open", failing_open):
            result = backup(vault, target)
        assert (result.copied, result.unreadable) == (2, 1)

    def test_a_read_that_fails_half_way_is_counted_as_unreadable(
        self, vault: Path, target: Path
    ) -> None:
        class Broken(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise OSError(5, "input/output error")

        real_open = Path.open

        def opener(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
            if mode == "rb" and str(vault) in str(self) and "blobs" in self.parts:
                return Broken()
            return real_open(self, mode, *args, **kwargs)  # type: ignore[call-overload]

        with mock.patch.object(Path, "open", opener):
            result = backup(vault, target)
        assert (result.copied, result.unreadable) == (0, 3)
        assert BlobStore(target / "blobs").scan().blobs == ()

    def test_a_copy_in_the_backup_with_the_wrong_size_is_kept_and_counted(
        self, vault: Path, target: Path
    ) -> None:
        data = DOCUMENTS["syllabus.txt"]
        target_store = BlobStore(target / "blobs")
        wrong = target_store.path_for(sha(data), "txt")
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b"short")
        result = backup(vault, target)
        assert (result.copied, result.mismatched) == (2, 1)
        assert wrong.read_bytes() == b"short"

    def test_two_names_for_one_hash_in_the_backup_are_counted(
        self, vault: Path, target: Path
    ) -> None:
        data = DOCUMENTS["syllabus.txt"]
        target_store = BlobStore(target / "blobs")
        for extension in ("txt", "md"):
            path = target_store.path_for(sha(data), extension)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        assert backup(vault, target).mismatched == 1

    def test_a_bad_row_in_the_snapshot_is_counted_not_trusted(
        self, vault: Path, target: Path
    ) -> None:
        rows: list[tuple[object, object, object]] = [
            ("../../outside", "txt", 3),
            (sha(b"x"), "TXT!", 1),
            (sha(b"x"), "txt", True),
        ]
        real = backup_module._snapshot

        def snapshot(db: Path, out: Path) -> list[tuple[object, object, object]]:
            real(db, out)
            return rows

        with mock.patch.object(backup_module, "_snapshot", snapshot):
            result = backup(vault, target)
        assert (result.damaged, result.copied) == (3, 0)

    def test_two_names_for_one_hash_in_the_vault_are_not_copied(
        self, vault: Path, target: Path
    ) -> None:
        data = DOCUMENTS["notes.md"]
        store = BlobStore(vault / "blobs")
        second = store.path_for(sha(data), "txt")
        second.write_bytes(data)
        result = backup(vault, target)
        assert (result.copied, result.damaged) == (2, 1)
        assert BlobStore(target / "blobs").find(sha(data)) is None

    def test_a_settings_file_that_cannot_be_read_stops_the_run(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        folder = tmp_path / "a-folder-not-a-file"
        folder.mkdir()
        with pytest.raises(BackupError, match="settings file"):
            backup(vault, target, config_file=folder)
        assert list((target / "snapshots").iterdir()) == []

    def test_a_damaged_vault_database_is_refused(self, vault: Path, target: Path) -> None:
        (vault / "vault.db").write_bytes(b"not a database at all" * 100)
        with pytest.raises(BackupError, match="SQLite could not"):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []

    def test_a_snapshot_that_fails_the_check_is_not_published(
        self, vault: Path, target: Path
    ) -> None:
        real_connect = sqlite3.connect

        class FailingCheck:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self._connection = connection

            def execute(self, sql: str, *args: Any) -> object:
                if sql.startswith("PRAGMA quick_check"):
                    return mock.Mock(fetchall=lambda: [("row 3 missing from index",)])
                return self._connection.execute(sql, *args)

            def close(self) -> None:
                self._connection.close()

        def connect(database: str, *args: object, **kwargs: object) -> object:
            connection = real_connect(database, *args, **kwargs)  # type: ignore[call-overload]
            return FailingCheck(connection) if database.endswith(".partial") else connection

        with (
            mock.patch.object(sqlite3, "connect", connect),
            pytest.raises(BackupError, match="did not pass"),
        ):
            backup(vault, target)
        assert list((target / "snapshots").iterdir()) == []


class TestASnapshotWhileTheAppWrites:
    def test_an_uncommitted_write_is_not_in_the_snapshot(self, vault: Path, target: Path) -> None:
        writer = sqlite3.connect(vault / "vault.db", isolation_level=None)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO app_meta (key, value) VALUES ('pending', 'yes')")
            result = backup(vault, target)
            writer.execute("COMMIT")
        finally:
            writer.close()
        copy = sqlite3.connect(target / "snapshots" / result.snapshot / "vault.db")
        try:
            assert copy.execute("SELECT * FROM app_meta WHERE key = 'pending'").fetchall() == []
        finally:
            copy.close()

    def test_a_snapshot_taken_during_steady_writes_is_consistent(
        self, vault: Path, target: Path
    ) -> None:
        """Each commit adds two rows that belong together; a consistent copy holds both of
        every pair, and the copy passes SQLite's integrity check."""
        stop = threading.Event()
        written = threading.Event()
        errors: list[BaseException] = []

        def write() -> None:
            connection = sqlite3.connect(vault / "vault.db", isolation_level=None, timeout=30)
            try:
                number = 0
                while not stop.is_set():
                    connection.execute("BEGIN IMMEDIATE")
                    for half in ("a", "b"):
                        connection.execute(
                            "INSERT INTO app_meta (key, value) VALUES (?, ?)",
                            (f"pair-{number}-{half}", "x" * 500),
                        )
                    connection.execute("COMMIT")
                    number += 1
                    if number == 20:
                        written.set()
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)
                written.set()
            finally:
                connection.close()

        thread = threading.Thread(target=write)
        thread.start()
        try:
            assert written.wait(30)
            result = backup(vault, target)
        finally:
            stop.set()
            thread.join(30)
        assert errors == []
        copy = sqlite3.connect(target / "snapshots" / result.snapshot / "vault.db")
        try:
            assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            keys = [row[0] for row in copy.execute("SELECT key FROM app_meta")]
        finally:
            copy.close()
        pairs = [key for key in keys if key.startswith("pair-")]
        assert len(pairs) >= 40
        assert len(pairs) % 2 == 0
        assert {k[:-2] for k in pairs if k.endswith("-a")} == {
            k[:-2] for k in pairs if k.endswith("-b")
        }


class TestAnInterruptedBackup:
    def test_a_full_disk_during_a_copy_leaves_no_partial_blob_and_no_snapshot(
        self, vault: Path, target: Path
    ) -> None:
        real_fsync = os.fsync
        calls = {"n": 0}

        def fsync(fd: int) -> None:
            calls["n"] += 1
            if calls["n"] == 2:  # the first is the snapshot; the second is the first blob
                raise OSError(28, "No space left on device")
            real_fsync(fd)

        with mock.patch.object(os, "fsync", fsync), pytest.raises(OSError, match="No space"):
            backup(vault, target)
        assert BlobStore(target / "blobs").scan().blobs == ()
        assert list((target / "blobs" / ".tmp").iterdir()) == []
        assert list((target / "snapshots").iterdir()) == []  # its own folder was removed
        result = backup(vault, target)
        assert (result.copied, result.problems, result.incomplete) == (3, 0, 0)

    def test_a_failure_after_the_blobs_removes_the_snapshot_folder(
        self, vault: Path, target: Path, config_file: Path
    ) -> None:
        with (
            mock.patch.object(backup_module, "publish_new", side_effect=OSError(5, "I/O error")),
            pytest.raises(OSError, match="I/O error"),
        ):
            backup(vault, target, config_file=config_file)
        assert list((target / "snapshots").iterdir()) == []
        assert len(BlobStore(target / "blobs").scan().blobs) == 3  # complete blobs stay

    def test_a_published_file_in_the_way_is_never_replaced(
        self, vault: Path, target: Path, config_file: Path
    ) -> None:
        with (
            mock.patch.object(backup_module, "publish_new", return_value=False),
            pytest.raises(BackupError, match="nothing was replaced"),
        ):
            backup(vault, target, config_file=config_file)

    def test_what_a_killed_run_left_is_tidied_or_counted_by_the_next(
        self, vault: Path, target: Path
    ) -> None:
        first = backup(vault, target)
        store = BlobStore(target / "blobs")
        (target / "blobs" / ".tmp" / "0123abcd").write_bytes(b"half a copy")
        blob = store.find(sha(DOCUMENTS["notes.md"]))
        assert blob is not None
        blob.chmod(0o644)  # killed between publishing and sealing
        killed = target / "snapshots" / "2026-10-17T120000Z"
        killed.mkdir()
        (killed / "vault.db.partial").write_bytes(b"part of a database")
        result = backup(vault, target, now=clock(START + timedelta(hours=1)))
        assert result.cleaned == 2
        assert result.incomplete == 1
        assert result.snapshot != first.snapshot
        assert list((target / "blobs" / ".tmp").iterdir()) == []
        assert is_read_only(blob)
        assert (killed / "vault.db.partial").exists()  # left for the owner

    def test_two_backups_to_one_target_take_turns(self, vault: Path, target: Path) -> None:
        backup(vault, target)
        with backup_module._target_lock(target), pytest.raises(BackupError, match="still running"):
            backup(vault, target, now=clock(START + timedelta(hours=1)))


class _Killed(BaseException):
    """Stands for the process being killed: raised at a crash window, with ``_undo`` patched out so
    nothing tidies up after it, as nothing would after a real kill."""


SNAPSHOT_MODES = {
    "vault.db": backup_module._sealed(0o444),
    "config.toml": backup_module._sealed(0o444),
    "local-token": backup_module._sealed(0o400),
}


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class TestAKillAfterPublishing:
    """Each file of a snapshot is linked to its final name, its temporary name is removed and only
    then is it made read-only. A kill in either window leaves a published file that is writable;
    the next run must give it its mode and remove a second name it left."""

    def kill(self, vault: Path, target: Path, config_file: Path, name: str, window: str) -> Path:
        real_publish = publish_new_blob
        real_chmod = Path.chmod

        def publish_new(temp: Path, final: Path) -> bool:
            published = real_publish(temp, final)
            if window == "linked" and final.name == name:
                raise _Killed  # the temporary name is still a second name of the file
            return published

        def chmod(self: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            if (
                window == "published"
                and self.name == name
                and self.parent.parent.name == "snapshots"
            ):
                raise _Killed  # published, temporary name removed, not yet read-only
            real_chmod(self, mode, follow_symlinks=follow_symlinks)

        with (
            mock.patch.object(backup_module, "publish_new", publish_new),
            mock.patch.object(Path, "chmod", chmod),
            mock.patch.object(backup_module, "_undo", return_value=None),
            pytest.raises(_Killed),
        ):
            backup(vault, target, config_file=config_file)
        (killed,) = (target / "snapshots").iterdir()
        return killed

    @pytest.mark.parametrize("window", ["linked", "published"])
    @pytest.mark.parametrize("name", sorted(SNAPSHOT_MODES))
    def test_the_next_run_seals_what_a_kill_left_writable(
        self, vault: Path, target: Path, config_file: Path, name: str, window: str
    ) -> None:
        killed = self.kill(vault, target, config_file, name, window)
        assert not is_read_only(killed / name)  # the state the finding describes
        if window == "linked":
            assert os.lstat(killed / name).st_nlink == 2  # the temporary name is kept
        result = backup(
            vault, target, config_file=config_file, now=clock(START + timedelta(hours=1))
        )
        assert result.cleaned == 1
        assert result.problems == 0
        assert mode_of(killed / name) == SNAPSHOT_MODES[name]
        assert os.lstat(killed / name).st_nlink == 1
        assert not (killed / (name + ".partial")).exists()
        # vault.db is published last: a kill before it leaves a snapshot counted as incomplete.
        assert result.incomplete == (0 if name == "vault.db" else 1)
        for file in (killed / "vault.db", killed / "config.toml", killed / "local-token"):
            if file.exists():
                assert mode_of(file) == SNAPSHOT_MODES[file.name]

    def test_every_mode_but_the_exact_one_is_repaired(
        self, vault: Path, target: Path, config_file: Path
    ) -> None:
        first = backup(vault, target, config_file=config_file)
        folder = target / "snapshots" / first.snapshot
        (folder / "vault.db").chmod(0o446)  # writable by anyone, not by the owner
        (folder / "config.toml").chmod(0o555)  # an execute bit
        (folder / "local-token").chmod(0o440)  # readable by the group
        result = backup(
            vault, target, config_file=config_file, now=clock(START + timedelta(hours=1))
        )
        # Windows keeps only the read-only attribute, which each of these modes sets.
        expected = 0 if os.name == "nt" else 3  # not acl.IS_WINDOWS, which conftest patches
        assert result.cleaned == expected
        for name, mode in SNAPSHOT_MODES.items():
            assert mode_of(folder / name) == mode
        again = backup(
            vault, target, config_file=config_file, now=clock(START + timedelta(hours=2))
        )
        assert again.cleaned == 0

    def test_a_snapshot_file_with_another_name_is_refused_and_left_alone(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        first = backup(vault, target)
        database = target / "snapshots" / first.snapshot / "vault.db"
        database.chmod(0o644)
        outside = tmp_path / "outside-copy"
        os.link(database, outside)
        before = mode_of(outside)
        with pytest.raises(BackupError, match="hard link"):
            backup(vault, target, now=clock(START + timedelta(hours=1)))
        assert mode_of(outside) == before

    def test_a_link_where_a_snapshot_file_belongs_is_refused(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        first = backup(vault, target)
        outside = tmp_path / "outside-file"
        outside.write_text("not a backup")
        outside.chmod(0o644)
        token = target / "snapshots" / first.snapshot / "local-token"
        token.chmod(0o600)
        token.unlink()
        link(outside, token)
        before = mode_of(outside)
        with pytest.raises(BackupError, match="snapshot's file"):
            backup(vault, target, now=clock(START + timedelta(hours=1)))
        assert mode_of(outside) == before

    def test_a_linked_snapshot_folder_is_not_entered(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        backup(vault, target)
        outside = tmp_path / "outside-folder"
        outside.mkdir()
        (outside / "vault.db").write_text("not a backup")
        (outside / "vault.db").chmod(0o644)
        link(outside, target / "snapshots" / "2026-01-01T000000Z")
        before = mode_of(outside / "vault.db")
        result = backup(vault, target, now=clock(START + timedelta(hours=1)))
        assert result.cleaned == 0
        assert mode_of(outside / "vault.db") == before


class TestTheTargetIsChecked:
    def test_a_relative_target_is_refused(self, vault: Path) -> None:
        with pytest.raises(BackupError, match="absolute"):
            check_target(Path("backups"), vault)

    @pytest.mark.parametrize("inside", ["", "backups", "blobs/sha256", "a/../b"])
    def test_a_target_inside_the_vault_is_refused(self, vault: Path, inside: str) -> None:
        target = vault / inside if inside else vault
        with pytest.raises(BackupError, match="inside the vault"):
            run_backup(vault, target)
        assert not (vault / "backups" / "snapshots").exists()

    def test_a_target_that_climbs_back_into_the_vault_is_refused(self, vault: Path) -> None:
        with pytest.raises(BackupError, match="inside the vault"):
            check_target(vault.parent / "elsewhere" / ".." / vault.name / "copy", vault)

    def test_a_target_inside_a_linked_vault_is_refused(self, vault: Path, tmp_path: Path) -> None:
        alias = link(vault, tmp_path / "vault-alias")
        with pytest.raises(BackupError, match="inside the vault"):
            check_target(vault / "copy", alias)

    @pytest.mark.parametrize("levels", [1, 2])
    def test_a_target_that_holds_the_vault_is_refused(self, vault: Path, levels: int) -> None:
        with pytest.raises(BackupError, match="vault folder is inside the backup target"):
            check_target(vault.parents[levels - 1], vault)

    def test_a_target_that_holds_a_linked_vault_is_refused(
        self, vault: Path, tmp_path: Path
    ) -> None:
        holder = tmp_path / "holder"
        holder.mkdir()
        (holder / "real-vault").mkdir()
        alias = link(holder / "real-vault", tmp_path / "vault-alias")
        with pytest.raises(BackupError, match="vault folder is inside the backup target"):
            check_target(holder, alias)

    def test_a_target_beside_the_vault_is_accepted(self, vault: Path) -> None:
        assert check_target(vault.parent / "vault-backup", vault) == vault.parent / "vault-backup"

    def test_a_target_inside_a_git_repository_is_refused(self, vault: Path, tmp_path: Path) -> None:
        repository = tmp_path / "repo"
        (repository / ".git").mkdir(parents=True)
        with pytest.raises(BackupError, match="Git repository"):
            check_target(repository / "backups", vault)

    @staticmethod
    def _resolving(given: Path, system: Path) -> Any:
        """Make ``Path.resolve`` give ``system`` for ``given``, as Windows does for a subst drive.

        A real link cannot stand in here: the target path check refuses a link before the Git
        check runs, while a subst drive or an 8.3 name is no link and still resolves elsewhere.
        """
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            if self == given:
                return system
            return real_resolve(self, strict=strict)

        return mock.patch.object(Path, "resolve", resolve)

    def test_a_target_that_resolves_into_a_git_repository_is_refused(
        self, vault: Path, tmp_path: Path
    ) -> None:
        repository = tmp_path / "repo"
        (repository / ".git").mkdir(parents=True)
        drive = tmp_path / "drive"  # the subst drive: outside every repository by its spelling
        drive.mkdir()
        with (
            self._resolving(drive / "backups", repository / "backups"),
            pytest.raises(BackupError, match="Git repository"),
        ):
            check_target(drive / "backups", vault)

    def test_a_target_inside_a_git_repository_only_by_its_spelling_is_refused(
        self, vault: Path, tmp_path: Path
    ) -> None:
        repository = tmp_path / "repo"
        (repository / ".git").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        with (
            self._resolving(repository / "backups", elsewhere / "backups"),
            pytest.raises(BackupError, match="Git repository"),
        ):
            check_target(repository / "backups", vault)

    def test_a_target_outside_every_repository_by_both_spellings_is_accepted(
        self, vault: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        drive = tmp_path / "drive"
        drive.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        with self._resolving(drive / "backups", elsewhere / "backups"):
            assert check_target(drive / "backups", vault) == drive / "backups"

    def test_a_file_in_the_target_path_is_refused(self, vault: Path, tmp_path: Path) -> None:
        (tmp_path / "a-file").write_text("x")
        with pytest.raises(BackupError, match="not a folder"):
            check_target(tmp_path / "a-file" / "backups", vault)

    @pytest.mark.parametrize("level", [0, 1, 2])
    def test_a_link_at_any_level_of_the_target_path_is_refused(
        self, vault: Path, tmp_path: Path, level: int
    ) -> None:
        outside = tmp_path / "outside"
        (outside / "b" / "c").mkdir(parents=True)
        (outside / "c").mkdir()
        parts = ["a", "b", "c"]
        parent = (tmp_path / "chain").joinpath(*parts[:level])
        parent.mkdir(parents=True)
        link(outside, parent / parts[level])
        with pytest.raises(BackupError, match="link"):
            run_backup(vault, (tmp_path / "chain").joinpath(*parts, "backups"))
        assert files_below(outside) == set()
        assert sorted(p.name for p in outside.rglob("*")) == ["b", "c", "c"]

    def test_the_target_itself_as_a_link_is_refused(self, vault: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = link(outside, tmp_path / "target")
        with pytest.raises(BackupError, match="link"):
            run_backup(vault, target)
        assert list(outside.iterdir()) == []

    def test_a_dangling_link_as_the_target_is_refused(self, vault: Path, tmp_path: Path) -> None:
        target = link(tmp_path / "nowhere", tmp_path / "target")
        with pytest.raises(BackupError, match="link"):
            run_backup(vault, target)
        assert not (tmp_path / "nowhere").exists()

    @pytest.mark.parametrize("name", ["blobs", "snapshots"])
    def test_a_link_where_a_backup_folder_belongs_is_refused(
        self, vault: Path, target: Path, tmp_path: Path, name: str
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target.mkdir(parents=True)
        link(outside, target / name)
        with pytest.raises(BackupError, match="link"):
            run_backup(vault, target)
        assert list(outside.iterdir()) == []

    def test_a_link_inside_the_backup_blob_store_is_refused(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        link(outside, target / "blobs" / "sha256")
        with pytest.raises(BackupError, match="link"):
            backup(vault, target)
        assert list(outside.iterdir()) == []
        assert list((target / "snapshots").iterdir()) == []

    def test_a_link_as_the_backup_temporary_folder_is_refused(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not the backup's")
        link(outside, target / "blobs" / ".tmp")
        with pytest.raises(BackupError, match="link"):
            backup(vault, target)
        assert (outside / "keep.txt").read_text() == "not the backup's"

    def test_a_link_as_the_lock_file_is_refused(
        self, vault: Path, target: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("not the backup's")
        link(outside, target / "backup.lock")
        with pytest.raises(BackupError, match="lock file"):
            backup(vault, target)
        assert outside.read_text() == "not the backup's"

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
    def test_a_junction_in_the_target_path_is_refused(  # pragma: no cover - Windows only
        self, vault: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        winapi = importlib.import_module("_winapi")  # typed only on Windows
        winapi.CreateJunction(str(outside), str(tmp_path / "junction"))
        with pytest.raises(BackupError, match="link"):
            run_backup(vault, tmp_path / "junction" / "backups")
        assert list(outside.iterdir()) == []

    def test_a_vault_without_a_database_is_refused(self, tmp_path: Path, target: Path) -> None:
        empty = tmp_path / "empty-vault"
        empty.mkdir()
        with pytest.raises(BackupError, match="no database"):
            backup(empty, target)
        assert not (empty / "vault.db").exists()  # never created by the backup
        assert list((target / "snapshots").iterdir()) == []


class TestTheTargetPermissions:
    def test_off_windows_nothing_is_checked(self, vault: Path, target: Path) -> None:
        assert backup(vault, target).private is None

    def test_on_windows_a_target_that_stays_open_is_reported(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[bool] = []

        def refuse(folder: Path, *, created: bool) -> None:
            calls.append(created)
            raise acl.VaultAccessError("open to others: " + str(folder))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", refuse)
        result = backup(vault, target)
        assert result.private is False
        assert calls == [True]  # a new target is made private, as the vault is
        assert result.problems == 0  # a warning, not a refusal (see docs/launcher.md)

    def test_on_windows_a_private_target_is_reported_as_private(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        walked: list[Path] = []
        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda folder, *, created: None)
        monkeypatch.setattr(acl, "verify_tree", walked.append)
        target.mkdir(parents=True)
        assert backup(vault, target).private is True
        assert walked == [target]  # the whole tree below the folder, with the same walk

    def test_on_windows_an_open_entry_below_a_private_target_is_reported(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A private target folder is not a private backup: ``blobs/``, ``snapshots/`` or a file
        moved in keeps its own list, and new files inherit their folder's."""

        def open_below(root: Path) -> None:
            raise acl.VaultAccessError("1 files or folders below " + str(root))

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda folder, *, created: None)
        monkeypatch.setattr(acl, "verify_tree", open_below)
        result = backup(vault, target)
        assert result.private is False
        assert result.problems == 0  # a warning, as for an open target folder
        assert "Warning: other accounts" in "\n".join(cli.report(result))

    def test_the_tree_is_walked_after_this_run_has_written(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[set[str]] = []
        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", lambda folder, *, created: None)
        monkeypatch.setattr(acl, "verify_tree", lambda root: seen.append(files_below(root)))
        result = backup(vault, target)
        assert len(seen) == 1
        assert f"snapshots/{result.snapshot}/vault.db" in seen[0]
        assert f"snapshots/{result.snapshot}/local-token" in seen[0]
        assert len([name for name in seen[0] if name.startswith("blobs/sha256/")]) == 3

    def test_an_open_target_folder_is_not_walked_again(
        self, vault: Path, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(folder: Path, *, created: bool) -> None:
            raise acl.VaultAccessError("open")

        def never(root: Path) -> None:
            raise AssertionError("the answer is already no")

        monkeypatch.setattr(acl, "IS_WINDOWS", True)
        monkeypatch.setattr(acl, "secure_vault_folder", refuse)
        monkeypatch.setattr(acl, "verify_tree", never)
        assert backup(vault, target).private is False


@pytest.mark.skipif(os.name != "nt", reason="uses PowerShell and Windows access lists")
class TestRealWindowsTargetLists:  # pragma: no cover - runs on the Windows CI job only
    """The real access lists. conftest turns the Windows steps off for this module, so each test
    turns them on again."""

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(acl, "IS_WINDOWS", True)

    @staticmethod
    def grant(path: Path, sid: str, *, flags: str = "(OI)(CI)") -> None:
        # A leading * means "this SID", whatever language Windows is in. Files take no flags.
        subprocess.run(  # noqa: S603
            ["icacls", str(path), "/grant", f"*{sid}:{flags}R"],  # noqa: S607
            check=True,
            capture_output=True,
        )

    def test_a_new_target_and_everything_in_it_is_private(self, vault: Path, target: Path) -> None:
        assert backup(vault, target).private is True

    def test_an_open_snapshots_folder_in_a_private_target_is_reported(
        self, vault: Path, target: Path
    ) -> None:
        now = clock()
        assert run_backup(vault, target, now=now).private is True
        self.grant(target / "snapshots", "S-1-5-32-545")  # Users, inherited by new snapshots
        assert acl.is_private(acl.read_access(target))  # the folder itself is still private
        assert run_backup(vault, target, now=now).private is False

    def test_one_open_blob_deep_in_a_private_target_is_reported(
        self, vault: Path, target: Path
    ) -> None:
        now = clock()
        assert run_backup(vault, target, now=now).private is True
        blob = BlobStore(target / "blobs").find(sha(DOCUMENTS["notes.md"]))
        assert blob is not None
        self.grant(blob, "S-1-5-19", flags="")  # one named second account
        assert run_backup(vault, target, now=now).private is False


class TestTheCommandLine:
    @pytest.fixture
    def settings(self, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "settings.toml"
        path.write_text(f"[vault]\npath = {json.dumps(str(vault))}\n")  # a TOML basic string
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        monkeypatch.delenv(cli.TARGET_ENV_VAR, raising=False)
        return path

    @staticmethod
    def private_words(vault: Path, target: Path) -> list[str]:
        return [
            PRIVATE_NAME,
            str(target),
            str(vault),
            *DOCUMENTS,
            sha(DOCUMENTS["notes.md"]),
            TOKEN_TEXT,
        ]

    def test_it_prints_counts_and_never_a_name(
        self, settings: Path, vault: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main(["--to", str(target)]) == 0
        out, err = capsys.readouterr()
        assert "Original files copied: 3" in out
        assert "Original files already in the backup: 0" in out
        assert err == ""
        for word in self.private_words(vault, target):
            assert word not in out + err
        snapshot = next((target / "snapshots").iterdir())
        assert (snapshot / "config.toml").read_bytes() == settings.read_bytes()

    def test_the_folder_can_come_from_the_environment(
        self,
        settings: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(cli.TARGET_ENV_VAR, str(target))
        assert cli.main([]) == 0
        assert "Original files copied: 3" in capsys.readouterr().out

    def test_without_a_folder_it_says_how_to_give_one(
        self, settings: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cli.main([]) == 1
        assert "pass --to" in capsys.readouterr().err

    def test_problems_are_counted_and_fail_the_run(
        self, settings: Path, vault: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        BlobStore(vault / "blobs").delete(sha(DOCUMENTS["notes.md"]))
        assert cli.main(["--to", str(target)]) == 1
        out, err = capsys.readouterr()
        assert "not in the vault: 1" in out
        assert "some files were not copied" in err
        for word in self.private_words(vault, target):
            assert word not in out + err

    def test_a_refusal_is_fixed_text(
        self, settings: Path, vault: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = vault / PRIVATE_NAME
        assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "inside the vault folder" in err
        for word in self.private_words(vault, target):
            assert word not in err

    def test_an_error_this_package_does_not_own_shows_only_its_class(
        self, settings: Path, vault: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        error = FileNotFoundError(2, "No such file", str(target / "secret.txt"))
        with mock.patch.object(cli, "run_backup", side_effect=error):
            assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        for word in self.private_words(vault, target):
            assert word not in err

    def test_an_interruption_is_reported_without_a_traceback(
        self, settings: Path, target: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch.object(cli, "run_backup", side_effect=KeyboardInterrupt):
            assert cli.main(["--to", str(target)]) == cli.EXIT_INTERRUPTED
        assert "interrupted" in capsys.readouterr().err

    def test_settings_that_cannot_be_read_are_named_by_class_only(
        self,
        tmp_path: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        missing = tmp_path / PRIVATE_NAME / "config.toml"
        monkeypatch.setenv("AIVAULT_CONFIG", str(missing))
        assert cli.main(["--to", str(target)]) == 1
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        assert PRIVATE_NAME not in err

    def test_settings_without_a_vault_are_refused(
        self,
        tmp_path: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "settings.toml"
        path.write_text("[server]\nport = 8765\n")
        monkeypatch.setenv("AIVAULT_CONFIG", str(path))
        assert cli.main(["--to", str(target)]) == 1
        assert "no vault folder is set" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["--to", "D:\\Clients\\" + PRIVATE_NAME, PRIVATE_NAME],  # an extra word
            ["--" + PRIVATE_NAME],  # an unknown option
            ["--to"],  # a missing value
            ["--t", PRIVATE_NAME],  # an abbreviation
            [PRIVATE_NAME],  # a positional word
        ],
    )
    def test_a_malformed_command_line_never_repeats_what_was_typed(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 2
        out, err = capsys.readouterr()
        assert "command line is not valid" in err
        assert PRIVATE_NAME not in out + err

    def test_the_report_names_every_problem(self) -> None:
        result = BackupResult(
            snapshot="2026-10-17T143005Z", copied=1, present=2, missing=3, damaged=4,
            unreadable=5, mismatched=6, incomplete=7, cleaned=8, private=False,
            token_missing=True,
        )  # fmt: skip
        text = "\n".join(cli.report(result))
        for figure in ("missing", "damaged", "unreadable", "mismatched", "incomplete"):
            assert str(getattr(result, figure)) in text
        assert "tidied up: 8" in text
        assert "Warning: other accounts" in text
        assert "no local-token yet" in text
        assert result.problems == 3 + 4 + 5 + 6


class TestPutWithAnExpectedHash:
    def test_bytes_with_another_hash_are_not_stored(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path / "blobs")
        with pytest.raises(BlobMismatchError):
            store.put(io.BytesIO(b"other bytes"), "txt", expected_sha256=sha(b"expected"))
        assert store.scan().blobs == ()
        assert list((store.root / ".tmp").iterdir()) == []

    def test_the_expected_hash_must_be_a_hash(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="SHA-256"):
            BlobStore(tmp_path / "blobs").put(io.BytesIO(b"x"), "txt", expected_sha256="x\n")

    @pytest.mark.parametrize("size", [len(b"expected") - 1, len(b"expected") + 1])
    def test_bytes_of_another_size_are_not_stored(self, tmp_path: Path, size: int) -> None:
        store = BlobStore(tmp_path / "blobs")
        with pytest.raises(BlobMismatchError, match="size"):
            store.put(
                io.BytesIO(b"expected"),
                "txt",
                max_bytes=size + 1,
                expected_sha256=sha(b"expected"),
                expected_size=size,
            )
        assert store.scan().blobs == ()

    @pytest.mark.parametrize("size", [-1, True, 1.0])
    def test_the_expected_size_must_be_a_count(self, tmp_path: Path, size: object) -> None:
        with pytest.raises(ValueError, match="whole number"):
            BlobStore(tmp_path / "blobs").put(io.BytesIO(b"x"), "txt", expected_size=size)  # type: ignore[arg-type]

    def test_bytes_with_the_expected_hash_are_stored(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path / "blobs")
        blob = store.put(io.BytesIO(b"expected"), "txt", expected_sha256=sha(b"expected"))
        assert blob.created


def test_an_engine_is_not_needed_to_read_the_snapshot(vault: Path, target: Path) -> None:
    """The snapshot is a plain SQLite file in rollback-journal mode: opening it read-only creates
    no side files next to it (a restore can read it from a read-only folder)."""
    result = backup(vault, target)
    folder = target / "snapshots" / result.snapshot
    copy = sqlite3.connect(f"{(folder / 'vault.db').as_uri()}?mode=ro", uri=True)
    try:
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        copy.close()
    assert sorted(p.name for p in folder.iterdir()) == ["local-token", "vault.db"]

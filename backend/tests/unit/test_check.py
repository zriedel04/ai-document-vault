"""The integrity check (``python -m vault.check``): it finds each kind of damage, reports kinds,
counts and hashes only, never changes the vault, and stays correct while the server runs."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from vault import check
from vault.check import Report, check_vault, main
from vault.core.config import CONFIG_ENV_VAR
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.db.schema import blobs
from vault.ingest.importer import Importer, ImportRequest
from vault.storage.blobs import ORPHAN_FOLDER, BlobIntegrityError, BlobStore, Scan
from vault.storage.layout import ensure_vault

# Synthetic content, with names that look private so a leak would be visible in the output.
NOTES = b"Course syllabus\nWeek 1: introduction\nWeek 2: storage\n"
NOTES_SHA = hashlib.sha256(NOTES).hexdigest()
PRIVATE_NAME = "Patient-Diagnosis-Confidential.txt"
PRIVATE_FOLDER = "Clients Patient-Name"
OTHER = b"A second synthetic file, never recorded\n"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault folder with its database and one imported document, as a server would leave it."""
    root = tmp_path / PRIVATE_FOLDER / "vault"
    paths = ensure_vault(root)
    upgrade_to_latest(paths.db)
    engine = create_vault_engine(paths.db)
    try:
        source = tmp_path / "incoming" / PRIVATE_NAME
        source.parent.mkdir()
        source.write_bytes(NOTES)
        result = Importer(engine, BlobStore(paths.blobs)).import_file(
            ImportRequest(
                path=source,
                original_filename=PRIVATE_NAME,
                method="upload",
                batch_id=ids.new_ulid(),
            )
        )
        assert result.document_id is not None
    finally:
        engine.dispose()
    return paths.root


@pytest.fixture
def engine(vault: Path) -> Iterator[Engine]:
    engine = create_vault_engine(vault / "vault.db")
    try:
        yield engine
    finally:
        engine.dispose()


def store(vault: Path) -> BlobStore:
    return BlobStore(vault / "blobs")


def blob_path(vault: Path, sha256: str = NOTES_SHA) -> Path:
    found = store(vault).find(sha256)
    assert found is not None
    return found


def make_writable(path: Path) -> None:
    path.chmod(stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWUSR)


def link(target: Path, name: Path) -> None:
    name.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, name, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make symbolic links here")


def counts(vault: Path) -> dict[str, int]:
    return check_vault(vault).counts


def add_blob_row(engine: Engine, sha256: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            blobs.insert().values(
                sha256=sha256, size=1, ext="txt", mime="text/plain", created_at=datetime.now(UTC)
            )
        )


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    vault_path: Path | None,
) -> tuple[int, str, str]:
    config = tmp_path / "check-config.toml"
    lines = [] if vault_path is None else ["[vault]", f"path = {str(vault_path)!r}"]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_ENV_VAR, str(config))
    code = main([])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def snapshot_tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every entry under ``root`` with its mode and, for a file, its bytes. SQLite's side files of
    a WAL database are left out when they hold no database pages (an empty ``-wal``, and the
    ``-shm`` index, which is shared memory): a read-only reader creates them as any reader does."""
    result: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        side = path.name.endswith("-shm") or (
            path.name.endswith("-wal") and path.stat().st_size == 0
        )
        if side:
            continue
        status = os.lstat(path)
        data = path.read_bytes() if stat.S_ISREG(status.st_mode) else None
        result[str(path.relative_to(root))] = (status.st_mode, data)
    return result


class TestACleanVault:
    def test_a_vault_with_a_document_has_no_problems(self, vault: Path) -> None:
        report = check_vault(vault)
        assert report.counts == {}
        assert report.blobs_checked == 1
        assert report.versions == 1
        assert report.lines()[-1] == "No problems found."

    def test_a_new_vault_with_no_blobs_has_no_problems(self, tmp_path: Path) -> None:
        paths = ensure_vault(tmp_path / "fresh")
        upgrade_to_latest(paths.db)
        report = check_vault(paths.root)
        assert report.counts == {}
        assert report.blobs_checked == 0

    def test_the_command_exits_with_0(
        self,
        vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code, out, err = run_main(monkeypatch, tmp_path, capsys, vault)
        assert code == check.OK
        assert "No problems found." in out
        assert err == ""

    def test_it_runs_as_a_module_with_the_exit_code_of_the_result(
        self, vault: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "module-config.toml"
        config.write_text(f"[vault]\npath = {str(vault)!r}\n", encoding="utf-8")
        env = {**os.environ, CONFIG_ENV_VAR: str(config)}
        command = [sys.executable, "-m", "vault.check"]
        clean = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
            command, env=env, cwd=tmp_path, capture_output=True, text=True, check=False
        )
        assert clean.returncode == check.OK, clean.stderr
        assert "No problems found." in clean.stdout
        (vault / "blobs" / "sha256" / "zz").write_bytes(b"synthetic")
        damaged = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
            command, env=env, cwd=tmp_path, capture_output=True, text=True, check=False
        )
        assert damaged.returncode == check.PROBLEMS
        assert "[stray_other]" in damaged.stdout

    def test_a_healthy_index_db_is_checked_too(self, vault: Path) -> None:
        connection = sqlite3.connect(vault / "index.db")
        connection.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        report = check_vault(vault)
        assert report.counts == {}
        assert "index.db: checked" in report.lines()


class TestBlobs:
    def test_a_corrupted_blob_is_a_mismatch_named_by_its_hash(self, vault: Path) -> None:
        path = blob_path(vault)
        make_writable(path)
        path.write_bytes(NOTES.replace(b"Week 2", b"Week 9"))
        report = check_vault(vault)
        assert report.counts == {"blob_mismatch": 1}
        assert report.hashes["blob_mismatch"] == {NOTES_SHA}
        assert f"      sha256 {NOTES_SHA}" in report.lines()

    def test_a_missing_blob_is_a_version_without_its_blob(self, vault: Path) -> None:
        path = blob_path(vault)
        make_writable(path)
        path.unlink()
        report = check_vault(vault)
        assert report.counts == {"version_blob_missing": 1}
        assert report.hashes["version_blob_missing"] == {NOTES_SHA}

    def test_a_blob_that_cannot_be_read_is_reported_as_unreadable(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(_self: BlobStore, _sha256: str) -> bool:
            raise PermissionError(13, "denied", str(blob_path(vault)))

        monkeypatch.setattr(BlobStore, "verify", denied)
        report = check_vault(vault)
        assert report.counts == {"blob_unreadable": 1}
        assert report.blobs_checked == 0

    def test_a_blob_that_gained_a_second_name_during_the_check_is_unreadable(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def doubled(_self: BlobStore, _sha256: str) -> bool:
            raise BlobIntegrityError("more than one file is stored under this hash")

        monkeypatch.setattr(BlobStore, "verify", doubled)
        assert counts(vault) == {"blob_unreadable": 1}

    def test_a_blob_that_no_row_records_is_unrecorded(self, vault: Path) -> None:
        store(vault).put(io.BytesIO(OTHER), "txt")  # a copy whose import crashed before its rows
        report = check_vault(vault)
        assert report.counts == {"blob_unrecorded": 1}
        assert report.hashes["blob_unrecorded"] == {OTHER_SHA}

    def test_a_blob_with_a_row_but_no_version_is_unused(self, vault: Path, engine: Engine) -> None:
        store(vault).put(io.BytesIO(OTHER), "txt")
        add_blob_row(engine, OTHER_SHA)
        assert counts(vault) == {"blob_unused": 1}

    def test_a_row_with_no_file_and_no_version_is_reported(
        self, vault: Path, engine: Engine
    ) -> None:
        add_blob_row(engine, OTHER_SHA)
        assert counts(vault) == {"row_blob_missing": 1}

    def test_hashes_beyond_the_limit_are_counted_not_listed(self, vault: Path) -> None:
        for number in range(check.HASHES_SHOWN + 3):
            store(vault).put(io.BytesIO(f"synthetic copy {number}\n".encode()), "txt")
        lines = check_vault(vault).lines()
        assert sum(line.startswith("      sha256 ") for line in lines) == check.HASHES_SHOWN
        assert "      and 3 more" in lines


class TestStrays:
    def test_a_stray_file_is_reported(self, vault: Path) -> None:
        stray = vault / "blobs" / "sha256" / "zz-notes.txt"
        stray.write_bytes(b"synthetic")
        assert counts(vault) == {"stray_other": 1}

    def test_a_blob_in_the_wrong_folder_is_a_stray(self, vault: Path) -> None:
        wrong = vault / "blobs" / "sha256" / "00" / "00" / f"{OTHER_SHA}.txt"
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(OTHER)
        assert counts(vault) == {"stray_other": 1}

    def test_a_link_in_place_of_a_blob_is_a_link_and_the_blob_is_missing(
        self, vault: Path, tmp_path: Path
    ) -> None:
        path = blob_path(vault)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(NOTES)
        make_writable(path)
        path.unlink()
        link(outside, path)
        # A link is never the blob, whatever it points at: the version has no blob either.
        assert counts(vault) == {"stray_link": 1, "version_blob_missing": 1}

    def test_a_link_beside_the_blobs_is_reported_and_not_followed(
        self, vault: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside-folder"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"synthetic")
        link(outside, vault / "blobs" / "sha256" / "ab")
        assert counts(vault) == {"stray_link": 1}

    def test_a_dangling_link_is_reported(self, vault: Path, tmp_path: Path) -> None:
        link(tmp_path / "gone", vault / "blobs" / "sha256" / "dangling")
        assert counts(vault) == {"stray_link": 1}

    def test_a_hard_link_to_a_blob_is_reported_once(self, vault: Path, tmp_path: Path) -> None:
        try:
            os.link(blob_path(vault), tmp_path / "outside.txt")
        except OSError:
            pytest.skip("this file system cannot make hard links")
        report = check_vault(vault)
        assert report.counts == {"stray_hard_link": 1}
        assert report.hashes["stray_hard_link"] == {NOTES_SHA}

    def test_one_hash_under_two_names_is_one_duplicate(self, vault: Path) -> None:
        path = blob_path(vault)
        (path.parent / f"{NOTES_SHA}.md").write_bytes(NOTES)
        report = check_vault(vault)
        assert report.counts == {"stray_duplicate": 1}
        assert report.hashes["stray_duplicate"] == {NOTES_SHA}

    def test_a_store_folder_that_is_a_file_is_reported(self, tmp_path: Path) -> None:
        paths = ensure_vault(tmp_path / "fresh")
        upgrade_to_latest(paths.db)
        (paths.blobs / "sha256").write_bytes(b"synthetic")
        assert check_vault(paths.root).counts == {"stray_other": 1}


class TestDatabases:
    def test_a_missing_database_is_reported(self, vault: Path) -> None:
        (vault / "vault.db").unlink()
        report = check_vault(vault)
        assert report.counts == {"vault_db_missing": 1}
        assert "The database and the blob store were not compared." in report.lines()
        assert report.blobs_checked == 1  # the blobs are still re-hashed

    def test_a_file_that_is_not_a_database_is_unreadable(self, vault: Path) -> None:
        (vault / "vault.db").write_bytes(b"synthetic damage " * 512)
        report = check_vault(vault)
        assert report.counts == {"vault_db_unreadable": 1}
        assert "The database and the blob store were not compared." in report.lines()

    def test_a_damaged_page_is_found_by_the_integrity_check(self, vault: Path) -> None:
        db = vault / "vault.db"
        connection = sqlite3.connect(db)
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        root = connection.execute(
            "SELECT rootpage FROM sqlite_schema WHERE name = 'ix_document_versions_blob_sha256'"
        ).fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        data = bytearray(db.read_bytes())
        start = (root - 1) * page_size
        # Keep the page header (a leaf index page), scramble its cells.
        data[start + 12 : start + page_size] = bytes([0x5A]) * (page_size - 12)
        db.write_bytes(bytes(data))
        found = counts(vault)
        assert found["vault_db_integrity"] > 0

    def test_a_row_whose_foreign_key_points_at_nothing_is_reported(self, vault: Path) -> None:
        connection = sqlite3.connect(vault / "vault.db")  # foreign keys are off by default
        connection.execute("UPDATE document_versions SET blob_sha256 = ?", (OTHER_SHA,))
        connection.commit()
        connection.close()
        found = counts(vault)
        assert found["vault_db_foreign_keys"] == 1
        # And the cross-check sees the same: the version's blob is not there, and the stored
        # blob is used by no version.
        assert found["version_blob_missing"] == 1
        assert found["blob_unused"] == 1

    def test_a_database_without_the_document_tables_is_reported(self, tmp_path: Path) -> None:
        paths = ensure_vault(tmp_path / "fresh")
        sqlite3.connect(paths.db).close()  # an empty database: no migration has run
        paths.db.write_bytes(paths.db.read_bytes())
        connection = sqlite3.connect(paths.db)
        connection.execute("CREATE TABLE something_else (x)")
        connection.commit()
        connection.close()
        assert check_vault(paths.root).counts == {"vault_db_tables": 1}

    def test_a_damaged_index_db_is_reported(self, vault: Path) -> None:
        (vault / "index.db").write_bytes(b"synthetic damage " * 512)
        assert counts(vault) == {"index_db_unreadable": 1}

    def test_a_vault_folder_with_marks_in_its_name_opens_its_database(self, tmp_path: Path) -> None:
        # A file: URI percent-encodes these, so a raw path in the URI would open another file.
        paths = ensure_vault(tmp_path / "José 100% #1")
        upgrade_to_latest(paths.db)
        assert check_vault(paths.root).counts == {}


class TestReadOnly:
    def test_nothing_in_a_damaged_vault_is_changed(self, vault: Path, tmp_path: Path) -> None:
        path = blob_path(vault)
        make_writable(path)
        path.write_bytes(b"edited")  # a mismatch, and left writable: the check must not seal it
        store(vault).put(io.BytesIO(OTHER), "txt")  # unrecorded: the check must not move it
        (vault / "blobs" / "sha256" / "zz").write_bytes(b"synthetic")
        link(tmp_path, vault / "blobs" / "sha256" / "link")
        before = snapshot_tree(vault)
        report = check_vault(vault)
        assert report.problems >= 4
        assert snapshot_tree(vault) == before
        assert not (vault / "blobs" / ORPHAN_FOLDER).exists()

    def test_the_database_file_is_not_written(self, vault: Path) -> None:
        db = vault / "vault.db"
        before = (db.read_bytes(), os.stat(db).st_mtime_ns)
        check_vault(vault)
        assert (db.read_bytes(), os.stat(db).st_mtime_ns) == before

    def test_a_missing_database_is_not_created(self, vault: Path) -> None:
        (vault / "vault.db").unlink()
        check_vault(vault)
        assert not (vault / "vault.db").exists()

    def test_a_missing_index_db_is_not_created(self, vault: Path) -> None:
        check_vault(vault)
        assert not (vault / "index.db").exists()


class TestWhileTheServerRuns:
    def test_an_open_write_transaction_does_not_block_or_leak_into_the_check(
        self, vault: Path, engine: Engine
    ) -> None:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")  # the server holds the write lock
            connection.execute(text("DELETE FROM import_sources"))
            report = check_vault(vault)
            connection.rollback()
        assert report.counts == {}

    def test_a_blob_published_after_the_scan_is_not_missing(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scan ran before an import published its blob; the rows were read after.
        monkeypatch.setattr(BlobStore, "scan", lambda _self: Scan((), ()))
        assert counts(vault) == {}

    def test_a_blob_whose_rows_were_written_after_the_first_read_is_not_unrecorded(
        self, vault: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store(vault).put(io.BytesIO(OTHER), "txt")
        real = check._read_database
        calls = 0

        def read(db_file: Path) -> check._Snapshot:
            nonlocal calls
            calls += 1
            if calls == 2:  # the import commits between the first read and the second
                add_blob_row(engine, OTHER_SHA)
            return real(db_file)

        monkeypatch.setattr(check, "_read_database", read)
        # The row has no version yet in this test, so it is unused, but no longer unrecorded.
        assert "blob_unrecorded" not in counts(vault)

    def test_a_hash_that_gained_a_second_name_after_the_scan_is_not_missing(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = blob_path(vault)
        (path.parent / f"{NOTES_SHA}.md").write_bytes(NOTES)
        monkeypatch.setattr(BlobStore, "scan", lambda _self: Scan((), ()))
        assert counts(vault) == {}

    def test_a_second_read_that_fails_keeps_the_first_verdict(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store(vault).put(io.BytesIO(OTHER), "txt")
        real = check._read_database
        calls = 0

        def read(db_file: Path) -> check._Snapshot:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(db_file)

        monkeypatch.setattr(check, "_read_database", read)
        assert counts(vault) == {"blob_unrecorded": 1}

    def test_a_blob_removed_after_the_scan_is_not_reported(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        put = store(vault).put(io.BytesIO(OTHER), "txt")
        real_scan = BlobStore.scan

        def scan_then_remove(self: BlobStore) -> Scan:
            result = real_scan(self)
            BlobStore(self.root).delete(OTHER_SHA)  # a refused import removes its own copy
            return result

        monkeypatch.setattr(BlobStore, "scan", scan_then_remove)
        assert put.created
        assert counts(vault) == {}

    def test_an_entry_that_became_a_blob_after_the_scan_is_not_a_stray(
        self, vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = blob_path(vault)
        monkeypatch.setattr(BlobStore, "scan", lambda _self: Scan((), (path,)))
        report = check_vault(vault)
        assert report.counts == {}


class TestOutput:
    def test_no_name_or_path_is_printed(
        self,
        vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = blob_path(vault)
        make_writable(path)
        path.write_bytes(b"edited")
        (vault / "blobs" / "sha256" / PRIVATE_NAME).write_bytes(b"synthetic")
        code, out, err = run_main(monkeypatch, tmp_path, capsys, vault)
        assert code == check.PROBLEMS
        for private in (PRIVATE_NAME, PRIVATE_FOLDER, "Patient", str(vault), str(tmp_path)):
            assert private not in out + err
        assert "[blob_mismatch]" in out
        assert "[stray_other]" in out

    def test_every_kind_has_a_fixed_description(self) -> None:
        for kind, description in check.KINDS.items():
            assert kind.isidentifier()
            assert description

    def test_a_report_lists_problems_in_a_fixed_order(self) -> None:
        report = Report()
        report.add("blob_unused", sha256=OTHER_SHA)
        report.add("vault_db_integrity", 3)
        lines = report.lines()
        assert lines.index("Problems found: 4") < lines.index(
            "  3 errors SQLite's integrity check reports in vault.db [vault_db_integrity]"
        )
        assert lines[-2].endswith("[blob_unused]")


class TestCannotRun:
    def test_arguments_are_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([PRIVATE_NAME]) == check.CANNOT_RUN
        captured = capsys.readouterr()
        assert PRIVATE_NAME not in captured.out + captured.err

    def test_no_vault_folder_in_the_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _out, err = run_main(monkeypatch, tmp_path, capsys, None)
        assert code == check.CANNOT_RUN
        assert "no vault folder is set" in err

    def test_a_vault_folder_that_does_not_exist_is_not_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / PRIVATE_FOLDER
        code, out, err = run_main(monkeypatch, tmp_path, capsys, missing)
        assert code == check.CANNOT_RUN
        assert "the vault folder does not exist" in err
        assert PRIVATE_FOLDER not in out + err
        assert not missing.exists()

    def test_a_settings_file_that_is_missing_is_not_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / PRIVATE_FOLDER / "config.toml"))
        assert main([]) == check.CANNOT_RUN
        captured = capsys.readouterr()
        assert "the settings could not be read" in captured.err
        assert PRIVATE_FOLDER not in captured.out + captured.err

    def test_a_vault_folder_inside_a_repository_is_refused_with_fixed_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / PRIVATE_FOLDER
        (repo / ".git").mkdir(parents=True)
        code, out, err = run_main(monkeypatch, tmp_path, capsys, repo / "vault")
        assert code == check.CANNOT_RUN
        assert "inside a Git repository" in err
        assert PRIVATE_FOLDER not in out + err

    def test_an_unexpected_error_prints_only_its_class(
        self,
        vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def broken(_root: Path) -> Report:
            raise FileNotFoundError(2, "No such file", str(vault / PRIVATE_NAME))

        monkeypatch.setattr(check, "check_vault", broken)
        code, out, err = run_main(monkeypatch, tmp_path, capsys, vault)
        assert code == check.CANNOT_RUN
        assert "FileNotFoundError" in err
        assert PRIVATE_NAME not in out + err

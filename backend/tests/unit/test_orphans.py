"""The start-up sweep: blobs no row uses are moved to ``blobs/orphans/``, never deleted."""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import Engine

from vault.core.config import Settings
from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.ingest.importer import Importer, ImportRequest
from vault.ingest.orphans import OrphanSweep, sweep_orphans
from vault.main import create_app
from vault.storage.blobs import ORPHAN_FOLDER, BlobIntegrityError, BlobNotFoundError, BlobStore

KEPT = b"Course syllabus\nWeek 1: introduction\n"
LOST = b"A copy whose import crashed before its rows were written\n"
LOST_SHA = hashlib.sha256(LOST).hexdigest()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    path = tmp_path / "vault.db"
    upgrade_to_latest(path)
    engine = create_vault_engine(path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    root = tmp_path / "blobs"
    root.mkdir()
    return BlobStore(root)


def import_text(engine: Engine, store: BlobStore, tmp_path: Path, data: bytes = KEPT) -> Path:
    """A blob with its rows, the normal result of an import."""
    source = tmp_path / "incoming" / "notes.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    Importer(engine, store).import_file(
        ImportRequest(
            path=source, original_filename="notes.txt", method="upload", batch_id=ids.new_ulid()
        )
    )
    found = store.find(hashlib.sha256(data).hexdigest())
    assert found is not None
    return found


def crashed_copy(store: BlobStore, data: bytes = LOST, extension: str = "txt") -> Path:
    """A blob without rows: the copy finished and the transaction never ran."""
    return store.put(io.BytesIO(data), extension).path


def is_read_only(path: Path) -> bool:
    return stat.S_IMODE(os.lstat(path).st_mode) & 0o222 == 0


def link(target: Path, name: Path) -> None:
    name.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, name, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make symbolic links here")


def test_a_blob_without_a_row_is_moved_aside_unchanged_and_read_only(
    engine: Engine, store: BlobStore, tmp_path: Path
) -> None:
    kept = import_text(engine, store, tmp_path)
    lost = crashed_copy(store)

    assert sweep_orphans(engine, store) == OrphanSweep(moved=1, failed=0, strays=0)

    moved = store.root / ORPHAN_FOLDER / f"{LOST_SHA}.txt"
    assert moved.read_bytes() == LOST
    assert is_read_only(moved)
    assert not lost.exists()
    assert store.find(LOST_SHA) is None
    assert not lost.parent.exists()  # its empty hash folders went with it
    assert kept.read_bytes() == KEPT
    assert [sha for sha, _ in store.scan().blobs] == [hashlib.sha256(KEPT).hexdigest()]


def test_a_blob_with_a_row_stays(engine: Engine, store: BlobStore, tmp_path: Path) -> None:
    kept = import_text(engine, store, tmp_path)

    assert sweep_orphans(engine, store) == OrphanSweep(moved=0, failed=0, strays=0)
    assert kept.read_bytes() == KEPT
    assert not (store.root / ORPHAN_FOLDER).exists()


def test_the_row_is_matched_by_hash_not_by_the_name_the_file_has(
    engine: Engine, store: BlobStore, tmp_path: Path
) -> None:
    # The row says txt; the file was renamed by hand to .md. The hash still names it.
    kept = import_text(engine, store, tmp_path)
    renamed = kept.with_suffix(".md")
    os.rename(kept, renamed)

    assert sweep_orphans(engine, store).moved == 0
    assert renamed.read_bytes() == KEPT


def test_a_name_already_in_the_orphan_folder_is_kept_and_the_new_one_numbered(
    engine: Engine, store: BlobStore
) -> None:
    earlier = store.root / ORPHAN_FOLDER / f"{LOST_SHA}.txt"
    earlier.parent.mkdir()
    earlier.write_bytes(b"set aside after an earlier crash")
    crashed_copy(store)

    assert sweep_orphans(engine, store).moved == 1

    assert earlier.read_bytes() == b"set aside after an earlier crash"
    assert (earlier.parent / f"{LOST_SHA}.txt.1").read_bytes() == LOST


def test_the_orphan_folder_never_counts_as_part_of_the_store(
    engine: Engine, store: BlobStore
) -> None:
    crashed_copy(store)
    sweep_orphans(engine, store)

    assert store.scan().blobs == ()
    assert store.scan().strays == ()
    assert sweep_orphans(engine, store) == OrphanSweep(moved=0, failed=0, strays=0)


def test_a_link_in_place_of_the_orphan_folder_is_refused_and_nothing_moves(
    engine: Engine, store: BlobStore, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link(outside, store.root / ORPHAN_FOLDER)
    lost = crashed_copy(store)

    assert sweep_orphans(engine, store) == OrphanSweep(moved=0, failed=1, strays=0)
    assert lost.read_bytes() == LOST
    assert list(outside.iterdir()) == []
    with pytest.raises(BlobIntegrityError):
        store.set_aside(LOST_SHA)


def test_a_file_in_place_of_the_orphan_folder_is_refused(engine: Engine, store: BlobStore) -> None:
    (store.root / ORPHAN_FOLDER).write_bytes(b"not a folder")
    lost = crashed_copy(store)

    assert sweep_orphans(engine, store).failed == 1
    assert lost.read_bytes() == LOST


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_a_junction_in_place_of_the_orphan_folder_is_refused(
    engine: Engine, store: BlobStore, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    winapi = importlib.import_module("_winapi")  # typed only on Windows
    winapi.CreateJunction(str(outside), str(store.root / ORPHAN_FOLDER))
    lost = crashed_copy(store)

    assert sweep_orphans(engine, store).failed == 1
    assert lost.read_bytes() == LOST
    assert list(outside.iterdir()) == []


def test_a_link_where_a_blob_belongs_is_reported_and_left_alone(
    engine: Engine, store: BlobStore, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(LOST)
    entry = store.path_for(LOST_SHA, "txt")
    link(outside, entry)

    assert sweep_orphans(engine, store) == OrphanSweep(moved=0, failed=0, strays=1)
    assert entry.is_symlink()
    assert outside.read_bytes() == LOST


def test_one_hash_under_two_names_is_reported_and_left_alone(
    engine: Engine, store: BlobStore
) -> None:
    first = crashed_copy(store)
    second = first.with_suffix(".md")
    second.write_bytes(LOST)

    assert sweep_orphans(engine, store) == OrphanSweep(moved=0, failed=0, strays=2)
    assert first.exists()
    assert second.exists()


def test_a_move_that_fails_is_counted_and_the_others_still_move(
    engine: Engine, store: BlobStore
) -> None:
    crashed_copy(store)
    crashed_copy(store, b"another lost copy\n")
    real_rename = os.rename
    calls: list[Any] = []

    def first_fails(source: Any, target: Any) -> None:
        calls.append(source)
        if len(calls) == 1:
            raise PermissionError("held by another program")
        real_rename(source, target)

    with mock.patch("vault.storage.blobs.os.rename", side_effect=first_fails):
        assert sweep_orphans(engine, store) == OrphanSweep(moved=1, failed=1, strays=0)
    assert len(store.scan().blobs) == 1
    assert is_read_only(store.scan().blobs[0][1])


def test_setting_aside_a_missing_blob_is_an_error(store: BlobStore) -> None:
    with pytest.raises(BlobNotFoundError):
        store.set_aside(LOST_SHA)


def test_start_up_moves_a_crashed_copy_aside(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    settings = Settings.model_validate({"vault": {"path": str(vault_dir)}})
    create_app(settings)  # makes the vault
    store = BlobStore(vault_dir / "blobs")
    crashed_copy(store)

    app = create_app(settings)
    app.state.engine.dispose()

    assert store.find(LOST_SHA) is None
    assert (vault_dir / "blobs" / ORPHAN_FOLDER / f"{LOST_SHA}.txt").read_bytes() == LOST


def test_start_up_logs_counts_and_no_hash(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    vault_dir = tmp_path / "vault"
    settings = Settings.model_validate({"vault": {"path": str(vault_dir)}})
    create_app(settings).state.engine.dispose()
    crashed_copy(BlobStore(vault_dir / "blobs"))
    capfd.readouterr()

    create_app(settings).state.engine.dispose()

    out, err = capfd.readouterr()
    assert "blobs.orphans moved=1 failed=0 strays=0" in out + err
    assert LOST_SHA not in out + err

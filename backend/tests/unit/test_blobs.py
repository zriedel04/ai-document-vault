import hashlib
import importlib
import io
import os
import stat
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from vault.storage import blobs as blobs_module
from vault.storage.blobs import (
    BlobBusyError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
    normalise_extension,
)

HELLO = b"hello vault"
HELLO_SHA = hashlib.sha256(HELLO).hexdigest()
ABC_SHA = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"  # NIST test vector


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def put(store: BlobStore, data: bytes, extension: str = "txt") -> Path:
    return store.put(io.BytesIO(data), extension).path


def is_read_only(path: Path) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & 0o222 == 0


def link(target: Path, name: Path) -> Path:
    """A symbolic link at ``name`` to ``target``; the test is skipped where links cannot be made
    (Windows without the privilege)."""
    name.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, name, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make symbolic links here")
    return name


def outside_file(tmp_path: Path, data: bytes = HELLO) -> Path:
    """A writable file that is not in the store, with the content of a blob: the original that an
    import must never alter."""
    path = tmp_path / "outside" / "original.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o644)
    return path


class TestStoring:
    def test_the_hash_is_sha256_of_the_bytes(self, store: BlobStore) -> None:
        assert store.put(io.BytesIO(b"abc"), "txt").sha256 == ABC_SHA

    def test_the_file_goes_to_a_path_named_by_its_hash(self, store: BlobStore) -> None:
        blob = store.put(io.BytesIO(HELLO), "TXT")
        assert (
            blob.path == store.root / "sha256" / HELLO_SHA[:2] / HELLO_SHA[2:4] / f"{HELLO_SHA}.txt"
        )
        assert blob.path.read_bytes() == HELLO
        assert (blob.size, blob.created) == (len(HELLO), True)

    def test_a_blob_is_read_only(self, store: BlobStore) -> None:
        assert is_read_only(put(store, HELLO))

    def test_the_same_bytes_are_stored_once(self, store: BlobStore) -> None:
        first = store.put(io.BytesIO(HELLO), "txt")
        second = store.put(io.BytesIO(HELLO), "txt")
        assert (first.created, second.created) == (True, False)
        assert first.path == second.path
        assert len(store.scan().blobs) == 1

    def test_the_same_bytes_under_another_extension_are_still_one_blob(
        self, store: BlobStore
    ) -> None:
        first = store.put(io.BytesIO(HELLO), "txt")
        second = store.put(io.BytesIO(HELLO), "md")
        assert second.created is False
        assert second.path == first.path  # the name the first import chose is kept
        assert len(store.scan().blobs) == 1

    def test_no_extension_is_allowed(self, store: BlobStore) -> None:
        blob = store.put(io.BytesIO(HELLO))
        assert blob.path.name == HELLO_SHA

    def test_different_bytes_are_different_blobs(self, store: BlobStore) -> None:
        assert put(store, b"one") != put(store, b"two")
        assert len(store.scan().blobs) == 2

    def test_an_empty_file_is_a_blob(self, store: BlobStore) -> None:
        blob = store.put(io.BytesIO(b""), "txt")
        assert blob.sha256 == hashlib.sha256(b"").hexdigest()
        assert blob.size == 0

    def test_a_file_larger_than_one_read_is_hashed_whole(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vault.storage.blobs.CHUNK_BYTES", 7)
        data = bytes(range(256)) * 5
        blob = store.put(io.BytesIO(data), "bin")
        assert blob.sha256 == hashlib.sha256(data).hexdigest()
        assert blob.path.read_bytes() == data

    def test_a_file_on_disk_is_copied_and_the_source_is_untouched(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        source = tmp_path / "Report.PDF"
        source.write_bytes(HELLO)
        blob = store.put_file(source)
        assert blob.path.suffix == ".pdf"  # the source's own extension, lower-cased
        assert source.read_bytes() == HELLO
        assert not is_read_only(source)  # the owner's original is not changed

    def test_unicode_and_very_long_source_names_are_fine(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        for name in ("résumé — 履歴書 🙂.txt", "x" * 200 + ".txt", "name with  spaces .txt"):
            source = tmp_path / name
            source.write_bytes(name.encode())
            blob = store.put_file(source)
            assert blob.path.read_bytes() == name.encode()
            assert blob.path.name == f"{blob.sha256}.txt"  # the human name never reaches the store

    def test_concurrent_imports_of_the_same_bytes_store_one_blob(self, store: BlobStore) -> None:
        results = []

        def work() -> None:
            results.append(store.put(io.BytesIO(HELLO * 1000), "txt"))

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        assert sum(1 for r in results if r.created) == 1
        assert len({r.path for r in results}) == 1
        assert store.verify(results[0].sha256)
        assert list((store.root / ".tmp").iterdir()) == []


class TestOneBlobPerHash:
    """The extension is part of the file name, so two imports of the same bytes under different
    extensions do not collide on their own; the claim on the hash is what keeps it to one file."""

    @staticmethod
    def slow_link(monkeypatch: pytest.MonkeyPatch) -> None:
        """Widen the window between "is it stored?" and "publish it", as a busy disk does."""
        real = os.link

        def link(src: Path, dst: Path) -> None:
            time.sleep(0.05)
            real(src, dst)

        monkeypatch.setattr("vault.storage.blobs.os.link", link)

    def test_concurrent_imports_under_different_extensions_store_one_blob(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.slow_link(monkeypatch)
        results = []

        def work(extension: str) -> None:
            results.append(store.put(io.BytesIO(HELLO), extension))

        threads = [
            threading.Thread(target=work, args=(extension,))
            for extension in ("txt", "md", "pdf", "docx", "png", "")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 6
        assert sum(1 for r in results if r.created) == 1
        assert len({r.path for r in results}) == 1  # every caller is told the winner's real path
        assert results[0].path.exists()
        assert [path for _, path in store.scan().blobs] == [results[0].path]
        assert store.scan().strays == ()
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_delete_waits_for_an_import_of_the_same_bytes(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.slow_link(monkeypatch)
        importer = threading.Thread(target=lambda: store.put(io.BytesIO(HELLO), "txt"))
        importer.start()
        time.sleep(0.01)  # the import is now publishing
        deleted = store.delete(HELLO_SHA)
        importer.join()
        assert deleted is (not store.exists(HELLO_SHA))  # never a half-state: gone or kept whole

    def test_the_claim_is_released_when_the_import_fails(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(_path: Path) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr("vault.storage.blobs._read_only", fail)
        with pytest.raises(OSError, match="disk went away"):
            store.put(io.BytesIO(HELLO), "txt")
        assert list((store.root / ".tmp").iterdir()) == []  # no lock and no temporary file

    def test_a_lock_left_by_a_crash_stops_imports_until_start_up_cleans_it(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vault.storage.blobs.LOCK_WAIT_SECONDS", 0.05)
        store.root.joinpath(".tmp").mkdir(parents=True)
        (store.root / ".tmp" / f"{HELLO_SHA}.lock").write_bytes(b"")
        with pytest.raises(BlobBusyError):
            store.put(io.BytesIO(HELLO), "txt")
        assert not store.exists(HELLO_SHA)
        assert list((store.root / ".tmp").iterdir()) == [store.root / ".tmp" / f"{HELLO_SHA}.lock"]
        assert store.clean_temp() == 1
        assert store.put(io.BytesIO(HELLO), "txt").created is True

    def test_windows_saying_access_denied_for_a_name_being_deleted_means_busy(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows, creating a name whose deletion is pending raises PermissionError. This
        # happened in CI when eight imports raced; the importer must wait, not fail.
        real = os.open
        refused: list[str] = []

        def open_(path: str | Path, flags: int, *args: int) -> int:
            if str(path).endswith(".lock") and len(refused) < 3:
                refused.append(str(path))
                raise PermissionError(13, "Permission denied")
            return real(path, flags, *args)

        monkeypatch.setattr("vault.storage.blobs.IS_WINDOWS", True)
        monkeypatch.setattr("vault.storage.blobs.os.open", open_)
        assert store.put(io.BytesIO(HELLO), "txt").created is True
        assert len(refused) == 3
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_real_permission_error_elsewhere_is_not_mistaken_for_a_busy_lock(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def denied(*_args: object, **_kwargs: object) -> int:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("vault.storage.blobs.IS_WINDOWS", False)
        monkeypatch.setattr("vault.storage.blobs.os.open", denied)
        with pytest.raises(PermissionError):
            store.put(io.BytesIO(HELLO), "txt")

    def test_a_lock_that_cannot_be_removed_for_a_moment_is_retried_on_windows(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = Path.unlink
        refused: list[int] = []

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".lock") and len(refused) < 3:
                refused.append(1)
                raise PermissionError(13, "in use by another process")
            real(self, missing_ok=missing_ok)

        monkeypatch.setattr("vault.storage.blobs.IS_WINDOWS", True)
        monkeypatch.setattr(Path, "unlink", unlink)
        assert store.put(io.BytesIO(HELLO), "txt").created is True
        assert len(refused) == 3
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_lock_that_stays_locked_is_an_error_not_a_silent_leftover(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".lock"):
                raise PermissionError(13, "in use by another process")

        monkeypatch.setattr("vault.storage.blobs.IS_WINDOWS", True)
        monkeypatch.setattr("vault.storage.blobs.UNLINK_TRIES", 3)
        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError):
            store.put(io.BytesIO(HELLO), "txt")

    def test_a_refused_removal_elsewhere_is_not_retried(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self.name.endswith(".lock"):
                calls.append(1)
                raise PermissionError(13, "denied")

        monkeypatch.setattr("vault.storage.blobs.IS_WINDOWS", False)
        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(PermissionError):
            store.put(io.BytesIO(HELLO), "txt")
        assert calls == [1]

    def test_other_hashes_are_not_blocked_by_a_lock(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vault.storage.blobs.LOCK_WAIT_SECONDS", 0.05)
        store.root.joinpath(".tmp").mkdir(parents=True)
        (store.root / ".tmp" / f"{HELLO_SHA}.lock").write_bytes(b"")
        assert store.put(io.BytesIO(b"other bytes"), "txt").created is True


class TestMalformedEntriesInTheWay:
    def test_a_folder_in_the_blobs_place_is_an_error_not_a_blob(self, store: BlobStore) -> None:
        folder = store.path_for(HELLO_SHA, "txt")
        folder.mkdir(parents=True)
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        assert not store.exists(HELLO_SHA)
        assert folder.is_dir()  # not deleted or replaced: that is the owner's call
        assert list((store.root / ".tmp").iterdir()) == []

    def test_the_error_names_neither_a_file_nor_a_path(self, store: BlobStore) -> None:
        store.path_for(HELLO_SHA, "txt").mkdir(parents=True)
        with pytest.raises(BlobIntegrityError) as caught:
            store.put(io.BytesIO(HELLO), "txt")
        assert str(store.root) not in str(caught.value)
        assert HELLO_SHA not in str(caught.value)

    def test_a_folder_in_the_way_on_a_system_without_links_is_the_same_error(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError("hard links are not supported here")

        def refused(_self: Path, _target: Path) -> Path:
            raise PermissionError  # how Windows answers a rename onto a folder

        store.path_for(HELLO_SHA, "txt").mkdir(parents=True)
        monkeypatch.setattr("vault.storage.blobs.os.link", no_links)
        monkeypatch.setattr(Path, "rename", refused)
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")

    def test_a_refused_rename_with_nothing_in_the_way_is_a_real_error(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError("hard links are not supported here")

        def refused(_self: Path, _target: Path) -> Path:
            raise PermissionError

        monkeypatch.setattr("vault.storage.blobs.os.link", no_links)
        monkeypatch.setattr(Path, "rename", refused)
        with pytest.raises(PermissionError):
            store.put(io.BytesIO(HELLO), "txt")

    def test_a_real_blob_under_another_extension_still_wins_over_the_odd_entry(
        self, store: BlobStore
    ) -> None:
        winner = put(store, HELLO, "md")
        store.path_for(HELLO_SHA, "txt").mkdir()  # an odd entry next to the real blob
        again = store.put(io.BytesIO(HELLO), "txt")
        assert (again.created, again.path) == (False, winner)


class TestEveryBlobIsReadOnlyAfterACrash:
    def crashed_before_marking(self, store: BlobStore, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Kill an import between linking the blob and marking it read-only."""

        def killed(_path: Path) -> None:
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr("vault.storage.blobs._read_only", killed)
            with pytest.raises(KeyboardInterrupt):
                store.put(io.BytesIO(HELLO), "txt")
        path = store.path_for(HELLO_SHA, "txt")
        assert path.read_bytes() == HELLO
        assert not is_read_only(path)  # the state the crash leaves behind
        return path

    def test_start_up_seals_a_blob_that_a_crash_left_writable(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self.crashed_before_marking(store, monkeypatch)
        assert store.seal() == 1
        assert is_read_only(path)
        assert store.seal() == 0  # nothing left to do
        assert store.verify(HELLO_SHA)

    def test_sealing_reaches_every_blob_and_keeps_the_content(self, store: BlobStore) -> None:
        paths = [put(store, f"file {number}".encode()) for number in range(5)]
        for path in paths[1:4]:
            path.chmod(0o644)
        assert store.seal() == 3
        assert all(is_read_only(path) for path in paths)
        assert [path.read_bytes() for path in paths] == [f"file {n}".encode() for n in range(5)]

    @pytest.mark.skipif(os.name == "nt", reason="Windows files have only a read-only attribute")
    @pytest.mark.parametrize(
        "mode",
        [0o644, 0o664, 0o446, 0o646, 0o666, 0o200, 0o755, 0o4444, 0o440, 0o400, 0o000],
    )
    def test_a_blob_is_sealed_only_when_it_is_exactly_read_only(
        self, store: BlobStore, mode: int
    ) -> None:
        # Any other mode is repaired: a write bit of the group or anyone else (a restored file
        # may be 0o664 or 0o446), an execute or set-id bit, or a missing read bit.
        path = put(store, HELLO)
        path.chmod(mode)
        assert store.seal() == 1
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
        assert store.seal() == 0

    @pytest.mark.skipif(os.name == "nt", reason="Windows files have only a read-only attribute")
    @pytest.mark.parametrize("mode", [0o446, 0o664, 0o755])
    def test_importing_the_same_bytes_again_repairs_the_mode_too(
        self, store: BlobStore, mode: int
    ) -> None:
        path = put(store, HELLO)
        path.chmod(mode)
        again = store.put(io.BytesIO(HELLO), "txt")
        assert (again.created, again.path) == (False, path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o444

    def test_sealing_an_empty_or_missing_store_is_fine(self, store: BlobStore) -> None:
        assert store.seal() == 0

    def test_importing_the_same_bytes_again_also_seals_the_blob(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self.crashed_before_marking(store, monkeypatch)
        again = store.put(io.BytesIO(HELLO), "md")
        assert (again.created, again.path) == (False, path)
        assert is_read_only(path)


class TestSafety:
    @pytest.mark.parametrize(
        "extension", ["tx t", "../x", "a/b", "a\\b", "txt\n", "txt ", "x" * 11, "é", "t.x"]
    )
    def test_an_extension_that_could_change_the_path_is_refused(
        self, store: BlobStore, extension: str
    ) -> None:
        with pytest.raises(ValueError, match="extension"):
            store.put(io.BytesIO(HELLO), extension)
        assert not store.root.exists() or store.scan().blobs == ()

    def test_extensions_are_normalised(self) -> None:
        assert normalise_extension(".PDF") == "pdf"
        assert normalise_extension("") == ""
        assert normalise_extension(".") == ""

    @pytest.mark.parametrize(
        "sha256",
        [
            "",
            "abc",
            HELLO_SHA.upper(),
            HELLO_SHA + "\n",
            HELLO_SHA + " ",
            HELLO_SHA[:-1],
            "../" * 22,
        ],
    )
    def test_a_hash_that_is_not_64_lower_case_hex_characters_is_refused(
        self, store: BlobStore, sha256: str
    ) -> None:
        for call in (store.find, store.exists, store.open, store.verify, store.delete):
            with pytest.raises(ValueError, match="SHA-256"):
                call(sha256)

    def test_a_file_over_the_limit_is_refused_and_leaves_nothing(self, store: BlobStore) -> None:
        with pytest.raises(BlobTooLargeError):
            store.put(io.BytesIO(b"x" * 11), "txt", max_bytes=10)
        assert store.scan().blobs == ()
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_file_exactly_at_the_limit_is_stored(self, store: BlobStore) -> None:
        assert store.put(io.BytesIO(b"x" * 10), "txt", max_bytes=10).created

    def test_the_limit_is_checked_while_reading_not_after(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vault.storage.blobs.CHUNK_BYTES", 4)
        reads: list[int] = []

        class Endless:
            def read(self, size: int) -> bytes:
                reads.append(size)
                return b"x" * size

        with pytest.raises(BlobTooLargeError):
            store.put(Endless(), "bin", max_bytes=10)  # type: ignore[arg-type]
        assert len(reads) < 10  # it stopped; an endless stream does not fill the disk


class TestCrashes:
    class Failing:
        """Delivers some bytes, then fails, as a disk or a network share can."""

        def __init__(self) -> None:
            self.sent = False

        def read(self, size: int) -> bytes:
            if not self.sent:
                self.sent = True
                return b"first"
            raise OSError("disk went away")

    def test_a_copy_that_fails_midway_leaves_no_blob_and_no_temporary_file(
        self, store: BlobStore
    ) -> None:
        with pytest.raises(OSError, match="disk went away"):
            store.put(self.Failing(), "txt")  # type: ignore[arg-type]
        assert store.scan().blobs == ()
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_temporary_file_left_by_a_killed_process_is_cleaned_up_not_mistaken_for_a_blob(
        self, store: BlobStore
    ) -> None:
        put(store, HELLO)
        leftover = store.root / ".tmp" / "deadbeef"
        leftover.write_bytes(b"partial")
        assert store.scan().strays == ()  # not part of the content
        assert store.clean_temp() == 1
        assert not leftover.exists()
        assert store.clean_temp() == 0
        assert store.exists(HELLO_SHA)  # the finished blob is untouched

    def test_cleaning_a_store_that_does_not_exist_yet_is_fine(self, store: BlobStore) -> None:
        assert store.clean_temp() == 0


class TestFileSystemsWithoutHardLinks:
    def test_the_copy_is_published_by_rename_instead(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError("hard links are not supported here")

        monkeypatch.setattr("vault.storage.blobs.os.link", no_links)
        blob = store.put(io.BytesIO(HELLO), "txt")
        assert blob.created is True
        assert blob.path.read_bytes() == HELLO
        assert is_read_only(blob.path)
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_name_taken_in_the_meantime_is_a_duplicate_there_too(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_links(*_args: object, **_kwargs: object) -> None:
            raise OSError("hard links are not supported here")

        original = Path.rename

        def taken(self: Path, target: Path) -> Path:
            if self.parent.name == ".tmp":
                target.write_bytes(HELLO)  # another process won the race
                raise FileExistsError
            return original(self, target)

        monkeypatch.setattr("vault.storage.blobs.os.link", no_links)
        monkeypatch.setattr(Path, "rename", taken)
        assert store.put(io.BytesIO(HELLO), "txt").created is False
        assert list((store.root / ".tmp").iterdir()) == []


class TestReading:
    def test_open_and_find(self, store: BlobStore) -> None:
        path = put(store, HELLO)
        assert store.find(HELLO_SHA) == path
        assert store.exists(HELLO_SHA)
        with store.open(HELLO_SHA) as stream:
            assert stream.read() == HELLO

    def test_a_missing_blob(self, store: BlobStore) -> None:
        assert store.find(HELLO_SHA) is None
        assert not store.exists(HELLO_SHA)
        with pytest.raises(BlobNotFoundError):
            store.open(HELLO_SHA)

    def test_verify_notices_damage_and_edits(self, store: BlobStore) -> None:
        path = put(store, HELLO)
        assert store.verify(HELLO_SHA)
        path.chmod(0o644)
        path.write_bytes(b"hello vaulT")  # the same length, one byte changed
        assert not store.verify(HELLO_SHA)

    def test_a_blob_for_another_hash_with_a_similar_name_is_not_found(
        self, store: BlobStore
    ) -> None:
        folder = store.folder_for(HELLO_SHA)
        folder.mkdir(parents=True)
        (folder / f"{HELLO_SHA}.txt.bak").write_bytes(b"x")
        (folder / f"{HELLO_SHA}-copy").write_bytes(b"x")
        (folder / f"{HELLO_SHA.upper()}.txt").write_bytes(b"x")
        assert store.find(HELLO_SHA) is None

    def test_a_folder_named_like_a_blob_is_not_a_blob(self, store: BlobStore) -> None:
        (store.folder_for(HELLO_SHA) / HELLO_SHA).mkdir(parents=True)
        assert store.find(HELLO_SHA) is None


class TestScan:
    def test_blobs_and_strays_are_both_reported(self, store: BlobStore) -> None:
        a = put(store, b"a")
        b = put(store, b"b", "pdf")
        sha_a = hashlib.sha256(b"a").hexdigest()
        misplaced = (
            store.root / "sha256" / "00" / "00" / f"{sha_a}.txt"
        )  # wrong folder for its name
        misplaced.parent.mkdir(parents=True)
        misplaced.write_bytes(b"a")
        odd = a.parent / "notes.txt"
        odd.write_bytes(b"?")
        scan = store.scan()
        assert {path for _, path in scan.blobs} == {a, b}
        assert set(scan.strays) == {misplaced, odd}
        assert sorted(store.iter_hashes()) == sorted(h for h, _ in scan.blobs)

    def test_an_empty_or_missing_store(self, store: BlobStore) -> None:
        assert store.scan().blobs == ()
        store.root.mkdir()
        assert store.scan().strays == ()


class TestDelete:
    def test_a_blob_is_removed_even_though_it_is_read_only(self, store: BlobStore) -> None:
        path = put(store, HELLO)
        assert store.delete(HELLO_SHA) is True
        assert not path.exists()
        assert not path.parent.exists()  # the folders only it used are gone
        assert (store.root / "sha256").is_dir()

    def test_neighbours_in_the_same_folder_survive(self, store: BlobStore) -> None:
        first = put(store, HELLO)
        sibling = first.parent / ("1" * 64)  # same prefix folder; no need for a real collision
        sibling.write_bytes(b"x")
        store.delete(HELLO_SHA)
        assert sibling.exists()

    def test_deleting_a_missing_blob_is_not_an_error(self, store: BlobStore) -> None:
        assert store.delete(HELLO_SHA) is False

    def test_the_same_bytes_can_be_imported_again_after_a_delete(self, store: BlobStore) -> None:
        put(store, HELLO)
        store.delete(HELLO_SHA)
        assert store.put(io.BytesIO(HELLO), "txt").created is True


class TestLinksAreNotBlobs:
    """A link where a blob or one of its folders belongs could lead outside the store. It is
    damage: never read as a blob, never changed, published into or deleted through, and reported."""

    @staticmethod
    def untouched(original: Path) -> None:
        assert original.read_bytes() == HELLO
        assert not is_read_only(original)  # the mode bits differ by system; only writable matters

    def test_a_link_in_the_place_of_a_blob_is_not_found_and_the_target_is_left_alone(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original = outside_file(tmp_path)
        entry = link(original, store.path_for(HELLO_SHA, "txt"))
        assert store.find(HELLO_SHA) is None
        assert not store.exists(HELLO_SHA)
        with pytest.raises(BlobNotFoundError):
            store.open(HELLO_SHA)
        self.untouched(original)
        assert entry.is_symlink()

    def test_importing_the_same_bytes_again_does_not_change_the_linked_file(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original = outside_file(tmp_path)
        entry = link(original, store.path_for(HELLO_SHA, "txt"))
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        # Under another extension the bytes are new to the store: a real blob is made beside the
        # link, which is still not a blob, and a later import finds that one.
        made = store.put(io.BytesIO(HELLO), "md")
        assert made.created is True
        assert made.path != entry
        assert store.put(io.BytesIO(HELLO), "txt").path == made.path
        self.untouched(original)  # the read-only mark would have followed the link
        assert entry.is_symlink()
        assert [path for _, path in store.scan().blobs] == [made.path]
        assert store.scan().strays == (entry,)
        assert list((store.root / ".tmp").iterdir()) == []

    def test_a_link_that_leads_nowhere_is_the_same(self, store: BlobStore, tmp_path: Path) -> None:
        entry = link(tmp_path / "missing", store.path_for(HELLO_SHA, "txt"))
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        assert entry.is_symlink()
        assert not (tmp_path / "missing").exists()

    def test_the_rename_fallback_does_not_replace_a_link_either(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original = outside_file(tmp_path)
        entry = link(original, store.path_for(HELLO_SHA, "txt"))
        with (
            mock.patch("os.link", side_effect=OSError("no hard links here")),
            pytest.raises(BlobIntegrityError),
        ):
            store.put(io.BytesIO(HELLO), "txt")
        assert entry.is_symlink()
        self.untouched(original)

    def test_sealing_and_deleting_do_not_go_through_a_link(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original = outside_file(tmp_path)
        link(original, store.path_for(HELLO_SHA, "txt"))
        assert store.seal() == 0
        assert store.delete(HELLO_SHA) is False
        self.untouched(original)

    def test_the_helpers_that_change_a_file_refuse_a_link(self, tmp_path: Path) -> None:
        original = outside_file(tmp_path)
        entry = link(original, tmp_path / "entry")
        for change in (blobs_module._read_only, blobs_module._writable, BlobStore._seal_file):
            with pytest.raises(BlobIntegrityError):
                change(entry)
        self.untouched(original)

    @pytest.mark.parametrize("level", [0, 1, 2])
    def test_a_link_in_place_of_a_folder_level_is_not_published_into(
        self, store: BlobStore, tmp_path: Path, level: int
    ) -> None:
        # level 0 is sha256/, 1 is sha256/aa, 2 is sha256/aa/bb
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        folder = store.folder_for(HELLO_SHA)
        levels = [folder.parent.parent, folder.parent, folder]
        link(outside, levels[level])
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        assert list(outside.iterdir()) == []  # nothing was published through the link
        assert levels[level].is_symlink()
        assert list((store.root / ".tmp").iterdir()) == []

    @pytest.mark.parametrize("level", [0, 1, 2])
    def test_a_real_blob_behind_a_linked_folder_is_not_found_sealed_or_deleted(
        self, store: BlobStore, tmp_path: Path, level: int
    ) -> None:
        # A blob that lives outside the store, reached through a folder link, is not the store's.
        folder = store.folder_for(HELLO_SHA)
        levels = [folder.parent.parent, folder.parent, folder]
        elsewhere = tmp_path / "elsewhere"
        blob_folder = elsewhere / folder.relative_to(levels[level])
        blob_folder.mkdir(parents=True)
        blob = blob_folder / f"{HELLO_SHA}.txt"
        blob.write_bytes(HELLO)
        blob.chmod(0o644)
        link(elsewhere, levels[level])
        assert store.find(HELLO_SHA) is None
        scan = store.scan()
        assert scan.blobs == ()
        assert levels[level] in scan.strays
        assert store.seal() == 0
        assert store.delete(HELLO_SHA) is False
        assert blob.read_bytes() == HELLO
        assert not is_read_only(blob)

    def test_a_vault_folder_that_is_itself_a_link_still_works(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        store = BlobStore(link(real, tmp_path / "vault-link"))
        stored = store.put(io.BytesIO(HELLO), "txt")
        assert stored.created is True
        assert store.find(HELLO_SHA) is not None
        assert store.scan().strays == ()
        assert (real / "sha256").is_dir()
        assert store.delete(HELLO_SHA) is True

    def test_a_windows_reparse_point_is_a_link_even_if_it_does_not_say_symlink(self) -> None:
        # A junction has st_file_attributes with the reparse bit and is not S_ISLNK.
        junction = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x10)
        regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x20)
        flagged_file = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x420)
        symlink = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)  # no attributes off Windows
        is_link, is_dir, is_file = (
            blobs_module._is_link,
            blobs_module._is_real_dir,
            blobs_module._is_real_file,
        )
        assert is_link(junction)  # type: ignore[arg-type]
        assert not is_dir(junction)  # type: ignore[arg-type]
        assert is_dir(folder)  # type: ignore[arg-type]
        assert is_file(regular)  # type: ignore[arg-type]
        assert not is_file(flagged_file)  # type: ignore[arg-type]
        assert is_link(symlink)  # type: ignore[arg-type]
        assert not is_dir(None)
        assert not is_file(None)

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
    def test_a_junction_is_treated_like_a_link(self, store: BlobStore, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        folder = store.folder_for(HELLO_SHA)
        folder.parent.parent.mkdir(parents=True)
        winapi = importlib.import_module("_winapi")  # typed only on Windows
        winapi.CreateJunction(str(outside), str(folder.parent))
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        assert list(outside.iterdir()) == []
        assert store.scan().strays == (folder.parent,)


class TestAFailedDeleteKeepsTheBlobReadOnly:
    @staticmethod
    def failing_unlink(blob: Path, error: BaseException) -> AbstractContextManager[object]:
        real = Path.unlink

        def unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == blob:
                raise error
            real(self, *args, **kwargs)  # type: ignore[arg-type]

        return mock.patch.object(Path, "unlink", unlink)

    @pytest.mark.parametrize(
        "error", [PermissionError("sharing violation"), OSError("disk error"), KeyboardInterrupt()]
    )
    def test_a_refused_delete_leaves_the_blob_read_only_and_intact(
        self, store: BlobStore, error: BaseException
    ) -> None:
        path = put(store, HELLO)
        assert is_read_only(path)
        with self.failing_unlink(path, error), pytest.raises(type(error)):
            store.delete(HELLO_SHA)
        assert is_read_only(path)
        assert path.read_bytes() == HELLO
        assert store.verify(HELLO_SHA)
        assert list((store.root / ".tmp").iterdir()) == []  # the claim was released too

    def test_a_blob_that_cannot_be_made_read_only_does_not_hide_the_original_error(
        self, store: BlobStore
    ) -> None:
        path = put(store, HELLO)

        def refuse(_path: Path) -> None:
            msg = "cannot change the mode"
            raise PermissionError(msg)

        with (
            self.failing_unlink(path, OSError("disk error")),
            mock.patch("vault.storage.blobs._read_only", refuse),
            pytest.raises(OSError, match="disk error"),
        ):
            store.delete(HELLO_SHA)

    def test_the_delete_still_works_afterwards(self, store: BlobStore) -> None:
        path = put(store, HELLO)
        with self.failing_unlink(path, PermissionError("busy")), pytest.raises(PermissionError):
            store.delete(HELLO_SHA)
        assert store.delete(HELLO_SHA) is True
        assert not path.exists()


class TestScanReportsWhatIsOdd:
    def test_valid_empty_structure_folders_are_not_reported(self, store: BlobStore) -> None:
        (store.root / "sha256" / "ab" / "cd").mkdir(parents=True)
        (store.root / "sha256" / "ef").mkdir()
        scan = store.scan()
        assert scan.blobs == ()
        assert scan.strays == ()

    def test_odd_folders_at_every_level_are_reported(self, store: BlobStore) -> None:
        base = store.root / "sha256"
        odd = [
            base / "zz",  # not two hex digits
            base / "ABC",
            base / "ab" / "x",
            base / "ab" / "cd" / "stuff",  # a folder where only blobs belong
            base / "ab" / "cd" / ("1" * 64),  # a folder named like a blob
            base / "ab" / "cd" / ("1" * 64 + ".txt"),
        ]
        for folder in odd:
            folder.mkdir(parents=True)
        (base / "zz" / "inside.txt").write_bytes(b"?")
        scan = store.scan()
        assert set(scan.strays) == set(odd)  # reported once, not entered
        assert scan.blobs == ()

    def test_a_folder_named_like_a_blob_does_not_hide_the_real_one(self, store: BlobStore) -> None:
        good = put(store, HELLO)
        (good.parent / ("2" * 64)).mkdir()
        scan = store.scan()
        assert {path for _, path in scan.blobs} == {good}
        assert scan.strays == (good.parent / ("2" * 64),)

    def test_links_at_every_level_are_reported_and_not_followed(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        real = put(store, HELLO)
        outside = tmp_path / "elsewhere"
        (outside / "ab").mkdir(parents=True)
        (outside / "file.txt").write_bytes(HELLO)
        base = store.root / "sha256"
        links = [
            link(outside, base / "cc"),
            link(outside, base / "dd" / "ee"),
            link(outside / "file.txt", real.parent / ("3" * 64 + ".txt")),
        ]
        scan = store.scan()
        assert {path for _, path in scan.blobs} == {real}
        assert set(scan.strays) == set(links)

    def test_a_file_where_the_folder_for_all_blobs_belongs_is_reported(
        self, store: BlobStore
    ) -> None:
        store.root.mkdir()
        (store.root / "sha256").write_bytes(b"?")
        assert store.scan().strays == (store.root / "sha256",)
        assert store.scan().blobs == ()

    def test_a_loose_file_at_each_level_is_reported(self, store: BlobStore) -> None:
        good = put(store, HELLO)
        loose = [
            store.root / "sha256" / "readme.txt",
            store.root / "sha256" / good.parent.parent.name / "loose.txt",
        ]
        for path in loose:
            path.write_bytes(b"?")
        assert set(store.scan().strays) == set(loose)


class TestTheTemporaryFolderIsARealFolder:
    """Copies, locks and the start-up clean-up all work inside ``.tmp``, so a link there must not
    lead them to another folder (the clean-up deletes files)."""

    @staticmethod
    def linked_tmp(store: BlobStore, tmp_path: Path) -> Path:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "original.txt").write_bytes(HELLO)
        (outside / "other.pdf").write_bytes(b"%PDF-1.4 other")
        store.root.mkdir()
        link(outside, store.root / ".tmp")
        return outside

    def test_the_clean_up_does_not_delete_through_a_linked_temporary_folder(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        outside = self.linked_tmp(store, tmp_path)
        with pytest.raises(BlobIntegrityError):
            store.clean_temp()
        assert sorted(path.name for path in outside.iterdir()) == ["original.txt", "other.pdf"]

    def test_an_import_does_not_copy_or_lock_through_it_either(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        outside = self.linked_tmp(store, tmp_path)
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        with pytest.raises(BlobIntegrityError):
            store.delete(HELLO_SHA)
        assert sorted(path.name for path in outside.iterdir()) == ["original.txt", "other.pdf"]
        assert not store.exists(HELLO_SHA)

    def test_a_file_in_the_place_of_the_temporary_folder_is_the_same_error(
        self, store: BlobStore
    ) -> None:
        store.root.mkdir()
        (store.root / ".tmp").write_bytes(b"?")
        with pytest.raises(BlobIntegrityError):
            store.clean_temp()
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")

    def test_a_link_inside_the_temporary_folder_is_removed_as_a_link_only(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "original.txt").write_bytes(HELLO)
        folder = store.root / ".tmp"
        folder.mkdir(parents=True)
        (folder / "leftover").write_bytes(b"copy")
        file_link = link(outside / "original.txt", folder / "a-link")
        folder_link = link(outside, folder / "a-folder-link")
        (folder / "a-folder").mkdir()
        removed = store.clean_temp()
        assert removed == 2  # the copy and the link to a file
        assert not file_link.is_symlink()
        assert folder_link.is_symlink()  # a link to a folder is left alone, with its target
        assert (folder / "a-folder").is_dir()
        assert (outside / "original.txt").read_bytes() == HELLO

    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
    def test_a_junction_as_the_temporary_folder_is_refused(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "original.txt").write_bytes(HELLO)
        store.root.mkdir()
        winapi = importlib.import_module("_winapi")  # typed only on Windows
        winapi.CreateJunction(str(outside), str(store.root / ".tmp"))
        with pytest.raises(BlobIntegrityError):
            store.clean_temp()
        assert (outside / "original.txt").read_bytes() == HELLO


class TestAFileWithAnotherNameIsNotABlob:
    """A hard link to a file outside the store has the same bytes and the same permissions, so
    changing the mode of the blob would change the outside original (ADR 0006)."""

    @staticmethod
    def shared(store: BlobStore, tmp_path: Path) -> tuple[Path, Path]:
        original = outside_file(tmp_path)
        entry = store.path_for(HELLO_SHA, "txt")
        entry.parent.mkdir(parents=True)
        try:
            os.link(original, entry)
        except OSError:
            pytest.skip("this file system has no hard links")
        return original, entry

    def test_it_is_not_found_not_sealed_and_not_a_blob_in_the_scan(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original, entry = self.shared(store, tmp_path)
        assert store.find(HELLO_SHA) is None
        assert store.seal() == 0
        scan = store.scan()
        assert scan.blobs == ()
        assert scan.strays == (entry,)
        assert not is_read_only(original)  # the outside file keeps its mode

    def test_an_import_of_the_same_bytes_does_not_change_the_outside_file(
        self, store: BlobStore, tmp_path: Path
    ) -> None:
        original, entry = self.shared(store, tmp_path)
        with pytest.raises(BlobIntegrityError):
            store.put(io.BytesIO(HELLO), "txt")
        assert not is_read_only(original)
        assert original.read_bytes() == HELLO
        assert os.path.samefile(original, entry)  # and the entry is left as it was
        assert store.delete(HELLO_SHA) is False
        assert original.exists()

    def test_the_helpers_refuse_it(self, store: BlobStore, tmp_path: Path) -> None:
        original, entry = self.shared(store, tmp_path)
        for change in (blobs_module._read_only, blobs_module._writable, BlobStore._seal_file):
            with pytest.raises(BlobIntegrityError):
                change(entry)
        assert not is_read_only(original)

    def test_a_blob_a_crash_left_linked_to_its_temporary_copy_is_sealed_after_the_clean_up(
        self, store: BlobStore
    ) -> None:
        # A crash between linking the blob and removing the temporary name leaves two names.
        blob = put(store, HELLO)
        blob.chmod(0o644)
        leftover = store.root / ".tmp" / "leftover"
        try:
            os.link(blob, leftover)
        except OSError:
            pytest.skip("this file system has no hard links")
        assert store.seal() == 0  # not yet a blob: it has another name
        assert store.clean_temp() == 1
        assert store.seal() == 1  # start-up order: clean_temp(), then seal()
        assert is_read_only(blob)
        assert store.verify(HELLO_SHA)

    @staticmethod
    def fake_status(links: int) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_nlink=links)

    def test_windows_gets_a_moment_for_a_removed_name_to_stop_counting(
        self, tmp_path: Path
    ) -> None:
        entry = tmp_path / "entry"
        answers = [self.fake_status(2), self.fake_status(2), self.fake_status(1)]
        with (
            mock.patch.object(blobs_module, "IS_WINDOWS", True),
            mock.patch.object(blobs_module, "_lstat", side_effect=answers),
        ):
            assert blobs_module._blob_status(entry) is answers[2]  # type: ignore[comparison-overlap]

    def test_a_count_that_stays_above_one_is_refused_on_windows_too(self, tmp_path: Path) -> None:
        entry = tmp_path / "entry"
        with (
            mock.patch.object(blobs_module, "IS_WINDOWS", True),
            mock.patch.object(blobs_module, "UNLINK_TRIES", 3),
            mock.patch.object(blobs_module, "_lstat", return_value=self.fake_status(2)) as lstat,
        ):
            assert blobs_module._blob_status(entry) is None
        assert lstat.call_count == 3

    def test_elsewhere_the_count_is_believed_at_once(self, tmp_path: Path) -> None:
        entry = tmp_path / "entry"
        with (
            mock.patch.object(blobs_module, "IS_WINDOWS", False),
            mock.patch.object(blobs_module, "_lstat", return_value=self.fake_status(2)) as lstat,
        ):
            assert blobs_module._blob_status(entry) is None
        assert lstat.call_count == 1

import hashlib
import io
import os
import stat
import threading
import time
from pathlib import Path

import pytest

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

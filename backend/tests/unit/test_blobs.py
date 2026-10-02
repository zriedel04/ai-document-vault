import hashlib
import io
import stat
import threading
from pathlib import Path

import pytest

from vault.storage.blobs import (
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

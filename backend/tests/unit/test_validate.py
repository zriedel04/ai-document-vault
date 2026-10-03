import os
import struct
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest import mock

import pytest

from vault.ingest.validate import (
    DEFAULT_LIMITS,
    EXTENSIONS,
    HEAD_BYTES,
    Kind,
    Limits,
    Reason,
    sniff,
    validate_file,
)

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
EXE = b"MZ\x90\x00" + b"\x00" * 60 + b"This program cannot be run in DOS mode."
OLE = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 32
TEXT = "Hello, résumé — 履歴書\nSecond line\n".encode()
CANARY = "canary-file-name-xyzzy"


def write(folder: Path, name: str, data: bytes) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def make_docx(
    path: Path,
    *,
    names: tuple[str, ...] = ("[Content_Types].xml", "word/document.xml"),
    body: bytes = b"<w:document/>",
) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, body)
    return path


class TestAccepted:
    @pytest.mark.parametrize(
        ("name", "data", "kind", "extension", "mime"),
        [
            ("a.pdf", PDF, Kind.PDF, "pdf", "application/pdf"),
            ("a.PDF", PDF, Kind.PDF, "pdf", "application/pdf"),
            ("a.png", PNG, Kind.PNG, "png", "image/png"),
            ("a.jpg", JPEG, Kind.JPG, "jpg", "image/jpeg"),
            ("a.JPEG", JPEG, Kind.JPG, "jpg", "image/jpeg"),  # stored as jpg
            ("a.txt", TEXT, Kind.TEXT, "txt", "text/plain"),
            ("a.md", TEXT, Kind.TEXT, "md", "text/markdown"),
            ("résumé 履歴書 🙂.txt", TEXT, Kind.TEXT, "txt", "text/plain"),
            ("x" * 200 + ".txt", TEXT, Kind.TEXT, "txt", "text/plain"),
        ],
    )
    def test_each_supported_kind(
        self, tmp_path: Path, name: str, data: bytes, kind: Kind, extension: str, mime: str
    ) -> None:
        verdict = validate_file(write(tmp_path, name, data))
        assert verdict.ok
        assert (verdict.detected, verdict.extension, verdict.mime) == (kind, extension, mime)

    def test_a_docx(self, tmp_path: Path) -> None:
        verdict = validate_file(make_docx(tmp_path / "a.docx"))
        assert (verdict.ok, verdict.detected, verdict.extension) == (True, Kind.DOCX, "docx")

    def test_every_allowed_extension_has_a_mime_type_and_a_test(self) -> None:
        assert set(EXTENSIONS) == {"pdf", "docx", "png", "jpg", "jpeg", "txt", "md"}

    @pytest.mark.parametrize(
        "data",
        [
            TEXT,
            b"\xef\xbb\xbf" + TEXT,  # UTF-8 with a byte order mark
            "UTF-16 text\n".encode("utf-16"),  # full of NUL bytes, but marked as text
            "caf\xe9 au lait\n".encode("latin-1"),  # not UTF-8, still text
            b"tabs\tand\r\nform\x0cfeeds\n",
            b"a",
        ],
    )
    def test_text_in_the_usual_encodings(self, tmp_path: Path, data: bytes) -> None:
        assert validate_file(write(tmp_path, "a.txt", data)).ok

    def test_a_pdf_may_start_after_white_space(self, tmp_path: Path) -> None:
        assert validate_file(write(tmp_path, "a.pdf", b"\r\n  " + PDF)).ok

    def test_a_file_exactly_at_the_size_limit_is_accepted(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.txt", b"x" * 10)
        assert validate_file(path, Limits(max_bytes=10)).ok


class TestRefused:
    @pytest.mark.parametrize(
        ("name", "data", "detected"),
        [
            ("a.pdf", PNG, Kind.PNG),  # a picture renamed to a PDF
            ("a.png", PDF, Kind.PDF),
            ("a.jpg", PNG, Kind.PNG),
            ("a.png", JPEG, Kind.JPG),
            ("a.pdf", EXE, Kind.BINARY),  # a program renamed to a PDF
            ("a.txt", EXE, Kind.BINARY),
            ("a.docx", EXE, Kind.BINARY),
            ("a.png", EXE, Kind.BINARY),
            ("a.pdf", TEXT, Kind.TEXT),  # text pretending to be a PDF
            ("a.docx", TEXT, Kind.TEXT),
            ("a.txt", PDF, Kind.PDF),  # a PDF renamed to .txt
            ("a.md", PNG, Kind.PNG),
            ("a.txt", b"PK\x03\x04" + b"\x00" * 30, Kind.ZIP),
            ("a.pdf", bytes([0] * 100), Kind.BINARY),
            ("a.txt", bytes(range(256)) * 4, Kind.BINARY),
            ("a.pdf", b"x" * 2000 + PDF, Kind.TEXT),  # "%PDF-" far from the start
            ("a.pdf", b"text then %PDF-1.7", Kind.TEXT),
        ],
    )
    def test_the_bytes_decide_not_the_name(
        self, tmp_path: Path, name: str, data: bytes, detected: Kind
    ) -> None:
        verdict = validate_file(write(tmp_path, name, data))
        assert not verdict.ok
        assert verdict.reason is Reason.CONTENT_MISMATCH
        assert verdict.detected is detected

    def test_a_zip_that_is_not_a_docx(self, tmp_path: Path) -> None:
        other = make_docx(tmp_path / "a.docx", names=("[Content_Types].xml", "xl/workbook.xml"))
        assert validate_file(other).reason is Reason.CONTENT_MISMATCH
        no_types = make_docx(tmp_path / "b.docx", names=("word/document.xml",))
        assert validate_file(no_types).reason is Reason.CONTENT_MISMATCH

    def test_a_docx_named_pdf_or_txt(self, tmp_path: Path) -> None:
        for name in ("a.pdf", "a.txt", "a.png"):
            path = make_docx(tmp_path / name)
            assert validate_file(path).reason is Reason.CONTENT_MISMATCH, name

    @pytest.mark.parametrize(
        "name",
        [
            "a.exe",
            "a.csv",
            "a.xlsx",
            "a.pptx",
            "a.doc",
            "a",
            "a.",
            ".pdf",
            "a.pdf.exe",
            "a.pdf\n",
            "a.pdf ",
            "a.p df",
            "a.tar.gz",
            "a.html",
            "a.svg",
            "a.zip",
            "a.bat",
        ],
    )
    def test_an_extension_that_is_not_on_the_list(self, tmp_path: Path, name: str) -> None:
        if os.name == "nt" and name.endswith((" ", ".")):
            pytest.skip("Windows drops a trailing space or dot from a file name")
        try:
            path = write(tmp_path, name, PDF)
        except OSError:
            pytest.skip("this file system cannot hold that name")
        assert validate_file(path).reason is Reason.UNSUPPORTED_EXTENSION

    def test_legacy_or_password_protected_office_files(self, tmp_path: Path) -> None:
        verdict = validate_file(write(tmp_path, "a.docx", OLE))
        assert verdict.reason is Reason.LEGACY_OR_ENCRYPTED_OFFICE
        assert validate_file(write(tmp_path, "b.pdf", OLE)).reason is Reason.CONTENT_MISMATCH

    def test_an_empty_file(self, tmp_path: Path) -> None:
        for name in ("a.txt", "a.pdf", "a.docx"):
            assert validate_file(write(tmp_path, name, b"")).reason is Reason.EMPTY

    def test_a_file_over_the_limit(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.txt", b"x" * 11)
        assert validate_file(path, Limits(max_bytes=10)).reason is Reason.TOO_LARGE
        assert validate_file(path).ok  # the default is far larger

    def test_the_size_is_checked_before_the_content(self, tmp_path: Path) -> None:
        path = write(tmp_path, "a.exe", b"x" * 11)
        assert validate_file(path, Limits(max_bytes=10)).reason is Reason.TOO_LARGE

    def test_not_a_file(self, tmp_path: Path) -> None:
        assert validate_file(tmp_path).reason is Reason.NOT_A_FILE
        assert validate_file(tmp_path / "missing.pdf").reason is Reason.NOT_A_FILE
        folder = tmp_path / "folder.pdf"
        folder.mkdir()
        assert validate_file(folder).reason is Reason.NOT_A_FILE

    def test_a_file_that_cannot_be_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write(tmp_path, "a.txt", TEXT)

        def denied(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("access is denied")

        monkeypatch.setattr(Path, "open", denied)
        assert validate_file(path).reason is Reason.UNREADABLE


class TestZipLimits:
    def docx(self, tmp_path: Path, **kwargs: object) -> Path:
        return make_docx(tmp_path / "a.docx", **kwargs)  # type: ignore[arg-type]

    def test_too_many_entries(self, tmp_path: Path) -> None:
        names = ("[Content_Types].xml", "word/document.xml", *(f"p/{i}.xml" for i in range(10)))
        path = self.docx(tmp_path, names=names)
        assert validate_file(path, Limits(max_zip_entries=5)).reason is Reason.UNSAFE_ARCHIVE
        assert validate_file(path, Limits(max_zip_entries=12)).ok

    def test_too_large_when_unpacked(self, tmp_path: Path) -> None:
        path = self.docx(tmp_path, body=b"a" * 1000)
        limits = Limits(max_zip_uncompressed_bytes=1500, max_zip_ratio=10**6)
        assert validate_file(path, limits).reason is Reason.UNSAFE_ARCHIVE

    def test_a_compression_bomb(self, tmp_path: Path) -> None:
        path = self.docx(tmp_path, body=b"\x00" * 2_000_000)  # a few KiB on disk
        assert path.stat().st_size < 10_000
        assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE  # the default ratio
        assert validate_file(path, Limits(max_zip_ratio=10**6)).ok

    def test_the_default_limits_accept_an_ordinary_document(self, tmp_path: Path) -> None:
        body = b"<w:p>some ordinary paragraph text</w:p>" * 20
        assert validate_file(self.docx(tmp_path, body=body)).ok
        assert DEFAULT_LIMITS.max_zip_ratio >= 100

    @pytest.mark.parametrize(
        "name",
        ["../evil.xml", "/etc/passwd", "a/../../b.xml", "..\\evil.xml", "C:/x.xml", "\\abs.xml"],
    )
    def test_member_names_that_climb_out(self, tmp_path: Path, name: str) -> None:
        path = self.docx(tmp_path, names=("[Content_Types].xml", "word/document.xml", name))
        assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_a_name_with_dots_inside_is_fine(self, tmp_path: Path) -> None:
        names = ("[Content_Types].xml", "word/document.xml", "word/media/a..b.png", "..x/y.xml")
        assert validate_file(self.docx(tmp_path, names=names)).ok

    def test_an_encrypted_entry(self, tmp_path: Path) -> None:
        path = self.docx(tmp_path)
        data = bytearray(path.read_bytes())
        # Set the "encrypted" bit in the central directory's flags (offset 8 of each record).
        index = data.find(b"PK\x01\x02")
        while index != -1:
            data[index + 8] |= 0x1
            index = data.find(b"PK\x01\x02", index + 1)
        path.write_bytes(bytes(data))
        assert validate_file(path).reason is Reason.ENCRYPTED_ARCHIVE

    def test_a_truncated_or_garbled_zip(self, tmp_path: Path) -> None:
        good = self.docx(tmp_path).read_bytes()
        for name, data in {
            "cut.docx": good[: len(good) // 2],
            "start-only.docx": good[:30],
            "garbled.docx": b"PK\x03\x04" + bytes(range(200)),
        }.items():
            verdict = validate_file(write(tmp_path, name, data))
            assert verdict.reason is Reason.UNREADABLE_ARCHIVE, name


class TestZipDirectoryIsBoundedBeforeItIsOpened:
    """``zipfile`` reads the whole central directory and builds an object per record before any
    count can be checked, so these files must be refused without ``zipfile`` ever opening them."""

    @staticmethod
    def never_opened() -> AbstractContextManager[object]:
        return mock.patch(
            "vault.ingest.validate.zipfile.ZipFile", side_effect=AssertionError("opened")
        )

    @staticmethod
    def docx(tmp_path: Path, extra: int = 0) -> Path:
        names = ("[Content_Types].xml", "word/document.xml", *(f"p/{i}.xml" for i in range(extra)))
        return make_docx(tmp_path / "a.docx", names=names)

    def test_too_many_entries_are_refused_without_opening_the_archive(self, tmp_path: Path) -> None:
        path = self.docx(tmp_path, extra=50)
        with self.never_opened():
            assert validate_file(path, Limits(max_zip_entries=10)).reason is Reason.UNSAFE_ARCHIVE

    def test_a_directory_over_the_byte_limit_is_refused_without_opening_the_archive(
        self, tmp_path: Path
    ) -> None:
        path = self.docx(tmp_path)
        with self.never_opened():
            verdict = validate_file(path, Limits(max_zip_directory_bytes=100))
        assert verdict.reason is Reason.UNSAFE_ARCHIVE
        assert validate_file(path).ok  # the default allows an ordinary document

    def test_the_real_entry_count_is_checked_too_if_the_claim_understates_it(
        self, tmp_path: Path
    ) -> None:
        # The end record says 2 entries, the directory holds 50: the claim passes, the count not.
        path = self.docx(tmp_path, extra=48)
        data = bytearray(path.read_bytes())
        end = len(data) - ZIP_END
        data[end + 8 : end + 12] = struct.pack("<HH", 2, 2)
        path.write_bytes(bytes(data))
        assert validate_file(path, Limits(max_zip_entries=10)).reason is Reason.UNSAFE_ARCHIVE

    def test_a_file_that_claims_millions_of_entries_in_a_small_file(self, tmp_path: Path) -> None:
        # The end record says 4 million entries and a 190 MiB directory in a 1 KiB file.
        record = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 190 << 20, 0, 0)
        path = write(tmp_path, "a.docx", b"PK\x03\x04" + b"\x00" * 1000 + record)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_zip64_claims_are_read_too(self, tmp_path: Path) -> None:
        # A ZIP64 end record carries the real counts when the plain record says 0xFFFF.
        body = b"PK\x03\x04" + b"\x00" * 1000
        wide = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 5, 5, 10**9, 0)
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(body), 1)
        plain = struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0
        )
        path = write(tmp_path, "a.docx", body + wide + locator + plain)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_a_zip64_archive_within_the_limits_is_still_opened(self, tmp_path: Path) -> None:
        path = self.docx(tmp_path)
        data = path.read_bytes()
        end = len(data) - ZIP_END
        _, _, _, _, entries, directory, offset, _ = struct.unpack("<4s4H2LH", data[end:])
        wide = struct.pack(
            "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, directory, offset
        )
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, end, 1)
        plain = struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0
        )
        path.write_bytes(data[:end] + wide + locator + plain)
        assert validate_file(path).ok

    @staticmethod
    def zip64_with_sector(
        tmp_path: Path,
        *,
        claim: tuple[int, int] | None = None,
        sector: int = 40,
        locator_to: int | None = None,
        disks: int = 1,
        keep_record: bool = True,
        plain_claims: tuple[int, int] = (2, 100),
    ) -> Path:
        """A real DOCX whose ZIP64 end record is followed by an extensible data sector (the case
        the fixed 56 bytes before the locator do not cover), with the locator naming the record."""
        real = TestZipDirectoryIsBoundedBeforeItIsOpened.docx(tmp_path).read_bytes()
        end = len(real) - ZIP_END
        _, _, _, _, entries, directory, offset, _ = struct.unpack("<4s4H2LH", real[end:])
        counted, sized = claim or (entries, directory)
        wide = struct.pack(
            "<4sQ2H2L4Q", b"PK\x06\x06", 44 + sector, 45, 45, 0, 0, counted, counted, sized, offset
        )
        if not keep_record:
            wide = b"\x00" * len(wide)
        locator = struct.pack(
            "<4sLQL", b"PK\x06\x07", 0, end if locator_to is None else locator_to, disks
        )
        plain = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, *plain_claims[::-1], 0)
        path = tmp_path / "sector.docx"
        path.write_bytes(real[:end] + wide + b"X" * sector + locator + plain)
        return path

    def test_an_honest_archive_with_an_extensible_data_sector_is_one_zipfile_opens(
        self, tmp_path: Path
    ) -> None:
        # The control: the layout of the next tests is one ``zipfile`` accepts, and a check that
        # only looks 56 bytes before the locator does not see its record at all.
        path = self.zip64_with_sector(tmp_path)
        with zipfile.ZipFile(path) as archive:
            assert "word/document.xml" in archive.namelist()
        assert validate_file(path).ok

    def test_the_record_behind_an_extensible_data_sector_is_the_one_judged(
        self, tmp_path: Path
    ) -> None:
        # The plain record claims 2 entries and 100 bytes; the ZIP64 record, which ``zipfile``
        # follows to, claims a 190 MiB directory.
        path = self.zip64_with_sector(tmp_path, claim=(4_000_000, 190 << 20))
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    @pytest.mark.parametrize("sector", [0, 1, 40, 5000])
    def test_any_size_of_sector_is_followed(self, tmp_path: Path, sector: int) -> None:
        path = self.zip64_with_sector(tmp_path, claim=(10, 190 << 20), sector=sector)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_a_locator_that_leads_to_no_record_is_unreadable_not_trusted(
        self, tmp_path: Path
    ) -> None:
        # The plain record is small and harmless; the locator is a claim that a ZIP64 record
        # exists, and ``zipfile`` would refuse the file only after reading its directory.
        path = self.zip64_with_sector(tmp_path, keep_record=False)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNREADABLE_ARCHIVE

    @pytest.mark.parametrize("locator_to", [10**9, 2**40])
    def test_a_locator_that_points_past_its_record_is_unreadable(
        self, tmp_path: Path, locator_to: int
    ) -> None:
        path = self.zip64_with_sector(tmp_path, locator_to=locator_to)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNREADABLE_ARCHIVE

    def test_an_archive_that_spans_disks_is_unreadable(self, tmp_path: Path) -> None:
        path = self.zip64_with_sector(tmp_path, disks=2)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNREADABLE_ARCHIVE

    def test_a_locator_offset_that_misses_falls_back_to_the_fixed_record_like_zipfile(
        self, tmp_path: Path
    ) -> None:
        # Data before the archive moves every offset; ``zipfile`` then uses the 56 bytes before the
        # locator. The offset names a place with no record (the start of the file).
        path = self.zip64_with_sector(tmp_path, sector=0, claim=(10, 190 << 20), locator_to=0)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_the_record_judged_is_the_one_zipfile_would_use(self, tmp_path: Path) -> None:
        # A hostile end record whose directory size field contains the bytes of another end-record
        # signature: searching for the last signature would judge the wrong record.
        size_field = int.from_bytes(b"PK\x05\x06", "little")  # about 101 million
        record = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, size_field, 0, 0)
        path = write(tmp_path, "a.docx", b"PK\x03\x04" + b"\x00" * 1000 + record)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_a_signature_hidden_in_the_comment_is_not_the_record(self, tmp_path: Path) -> None:
        # With a comment, zipfile takes the last signature in the final 64 KiB. A comment that
        # holds a fake record is the record it uses, and it is judged like any other.
        real = self.docx(tmp_path).read_bytes()
        fake = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 190 << 20, 0, 0)
        comment = b"x" * 5 + fake + b"y" * 5
        data = bytearray(real)
        data[-2:] = len(comment).to_bytes(2, "little")
        path = write(tmp_path, "b.docx", bytes(data) + comment)
        with self.never_opened():
            assert validate_file(path).reason is Reason.UNSAFE_ARCHIVE

    def test_a_file_with_no_end_record_is_unreadable(self, tmp_path: Path) -> None:
        for name, data in {
            "short.docx": b"PK\x03\x04" + b"\x00" * 10,
            "none.docx": b"PK\x03\x04" + b"\x00" * 5000,
            "cut-record.docx": b"PK\x03\x04" + b"\x00" * 100 + b"PK\x05\x06" + b"\x00" * 4,
        }.items():
            with self.never_opened():
                assert validate_file(write(tmp_path, name, data)).reason is (
                    Reason.UNREADABLE_ARCHIVE
                ), name

    def test_reading_the_end_record_reads_only_the_end_of_the_file(self, tmp_path: Path) -> None:
        # A multi-megabyte directory claim is judged from at most 64 KiB plus the record.
        record = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 5, 5, 100, 0, 0)
        path = write(tmp_path, "a.docx", b"PK\x03\x04" + b"\x00" * 300_000 + record)
        reads: list[int] = []
        real_open = Path.open

        def counting(self: Path, *args: object, **kwargs: object) -> object:
            stream = real_open(self, *args, **kwargs)  # type: ignore[call-overload]
            if self.name == "a.docx":
                original: Callable[[int], bytes] = stream.read

                def read(size: int = -1) -> bytes:
                    data = original(size)
                    reads.append(len(data))
                    return data

                stream.read = read
            return stream

        with mock.patch.object(Path, "open", counting):
            validate_file(path)
        assert max(reads) <= 70_000


ZIP_END = 22


class TestSniff:
    def test_each_signature(self) -> None:
        assert sniff(PDF) is Kind.PDF
        assert sniff(PNG) is Kind.PNG
        assert sniff(JPEG) is Kind.JPG
        assert sniff(b"PK\x03\x04rest") is Kind.ZIP
        assert sniff(OLE) is Kind.OLE
        assert sniff(TEXT) is Kind.TEXT
        assert sniff(EXE) is Kind.BINARY
        assert sniff(b"") is Kind.TEXT

    def test_a_signature_must_be_at_the_start(self) -> None:
        assert sniff(b" " + PNG) is not Kind.PNG
        assert sniff(b"x" + JPEG) is not Kind.JPG
        assert sniff(b"x" + b"PK\x03\x04") is not Kind.ZIP

    def test_the_pdf_header_needs_a_version(self) -> None:
        assert sniff(b"%PDF-1.4") is Kind.PDF
        assert sniff(b"%PDF-2.0") is Kind.PDF
        assert sniff(b"%PDF-") is Kind.TEXT
        assert sniff(b"%PDF-x.y") is Kind.TEXT
        assert sniff(b"%PDF-9.9") is Kind.TEXT
        assert sniff(b"x" * 20 + b"%PDF-1.4") is Kind.TEXT

    def test_one_nul_byte_without_a_byte_order_mark_is_binary(self) -> None:
        assert sniff(b"x" * 100 + b"\x00") is Kind.BINARY
        assert sniff("x".encode("utf-16")) is Kind.TEXT  # a byte order mark says it is text

    def test_a_little_binary_is_tolerated_a_lot_is_not(self) -> None:
        assert sniff(b"x" * 100 + b"\x01\x02") is Kind.TEXT  # about 2 percent
        assert sniff(b"x" * 90 + b"\x01" * 10) is Kind.BINARY

    def test_only_the_head_of_a_file_is_examined(self, tmp_path: Path) -> None:
        # The assumption, stated: text that turns binary after the first HEAD_BYTES passes here
        # and is caught when it is extracted.
        path = write(tmp_path, "a.txt", b"x" * HEAD_BYTES + b"\x00" * 1000)
        assert validate_file(path).ok
        path = write(tmp_path, "b.txt", b"x" * (HEAD_BYTES - 10) + b"\x00" * 1000)
        assert validate_file(path).reason is Reason.CONTENT_MISMATCH  # the NULs start inside it


class TestTextWithAByteOrderMark:
    """A byte order mark says how to decode what follows; it does not say that what follows is
    text, so the head is decoded and judged like text without a mark."""

    @pytest.mark.parametrize(
        "data",
        [
            b"\xef\xbb\xbfMZ\x00\x00\x00" + b"\x00" * 40,  # a program behind a UTF-8 mark
            b"\xef\xbb\xbf" + EXE,
            b"\xef\xbb\xbf\xff\xfe\xfd" + TEXT,  # not valid UTF-8
            b"\xef\xbb\xbf" + b"x" * 90 + b"\x01" * 10,  # mostly control characters
            b"\xff\xfe" + EXE,  # UTF-16 mark, and a program behind it
            b"\xfe\xff" + EXE,
            b"\xff\xfe\x00\xd8x\x00",  # a lone surrogate
            b"\xff\xfe\x00\x00" + b"\xff\xff\xff\xff",  # a UTF-32 mark and no valid character
            b"\xff\xfe" + "text".encode("utf-16-le") + b"\x00\x00",  # a NUL character
        ],
    )
    def test_binary_behind_a_mark_is_binary(self, tmp_path: Path, data: bytes) -> None:
        assert sniff(data) is Kind.BINARY
        verdict = validate_file(write(tmp_path, "a.txt", data))
        assert verdict.reason is Reason.CONTENT_MISMATCH
        assert verdict.detected is Kind.BINARY

    @pytest.mark.parametrize(
        ("encoding", "mark"),
        [
            ("utf-8", b"\xef\xbb\xbf"),
            ("utf-16-le", b"\xff\xfe"),
            ("utf-16-be", b"\xfe\xff"),
            ("utf-32-le", b"\xff\xfe\x00\x00"),
            ("utf-32-be", b"\x00\x00\xfe\xff"),
        ],
    )
    def test_real_text_in_each_marked_encoding_is_text(
        self, tmp_path: Path, encoding: str, mark: bytes
    ) -> None:
        data = mark + TEXT.decode().encode(encoding)
        assert sniff(data) is Kind.TEXT
        assert validate_file(write(tmp_path, "a.txt", data)).ok

    def test_a_mark_alone_is_an_empty_text(self) -> None:
        for mark in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
            assert sniff(mark) is Kind.TEXT

    def test_a_head_cut_in_the_middle_of_a_character_is_not_an_error(self) -> None:
        for encoding, mark in (("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe")):
            whole = ("é履" * 10).encode(encoding)
            for cut in (len(whole) - 1, len(whole) - 2):
                assert sniff(mark + whole[:cut], complete=False) is Kind.TEXT, (encoding, cut)

    @pytest.mark.parametrize(
        "data",
        [
            b"\xef\xbb\xbf\xc3",  # a UTF-8 mark and half of a character
            b"\xef\xbb\xbfabc\xe5\xb1",
            b"\xff\xfe\x41",  # a UTF-16 mark and one byte
            b"\xfe\xff\x00",
            b"\xff\xfe\x41\x00\x42",
            b"\xff\xfe\x00\xd8",  # a high surrogate and no low one
            b"\xff\xfe\x00\x00\x41\x00",  # a UTF-32 mark and half of a character
        ],
    )
    def test_a_file_that_ends_in_the_middle_of_a_character_is_binary(
        self, tmp_path: Path, data: bytes
    ) -> None:
        # When the head is the whole file there is nothing more to complete the character.
        assert sniff(data) is Kind.BINARY
        assert sniff(data, complete=False) is Kind.TEXT  # a cut head is another matter
        verdict = validate_file(write(tmp_path, "a.txt", data))
        assert verdict.reason is Reason.CONTENT_MISMATCH
        assert verdict.detected is Kind.BINARY

    def test_a_file_exactly_as_long_as_the_head_is_judged_whole(self, tmp_path: Path) -> None:
        body = b"x" * (HEAD_BYTES - 3)
        whole = b"\xef\xbb\xbf" + body
        assert validate_file(write(tmp_path, "a.txt", whole)).ok
        bad = write(tmp_path, "b.txt", b"\xef\xbb\xbf" + body[:-1] + b"\xc3")
        assert len(bad.read_bytes()) == HEAD_BYTES
        assert validate_file(bad).reason is Reason.CONTENT_MISMATCH

    def test_a_long_file_cut_in_the_middle_of_a_character_by_the_head_is_text(
        self, tmp_path: Path
    ) -> None:
        # The head ends inside the multi-byte character at the cut; the file goes on.
        body = b"x" * (HEAD_BYTES - 4) + "é".encode() + b"y" * 100
        data = b"\xef\xbb\xbf" + body
        assert data[HEAD_BYTES - 1 : HEAD_BYTES + 1] == "é".encode()
        assert validate_file(write(tmp_path, "a.txt", data)).ok

    def test_the_decoded_test_is_the_same_as_for_unmarked_text(self) -> None:
        few = b"x" * 100 + b"\x01\x02"
        assert sniff(b"\xef\xbb\xbf" + few) is Kind.TEXT  # about 2 percent, as without a mark
        assert sniff(few) is Kind.TEXT


class TestVerdictsAreSafeToShow:
    def test_a_verdict_never_contains_the_file_name_or_content(self, tmp_path: Path) -> None:
        for name, data in ((f"{CANARY}.pdf", PNG), (f"{CANARY}.txt", TEXT), (f"{CANARY}.exe", PDF)):
            verdict = validate_file(write(tmp_path, name, data))
            assert CANARY not in repr(verdict)
            assert "Hello" not in repr(verdict)

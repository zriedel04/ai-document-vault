"""``make-manifest``: listing the owner's folder into a manifest the embedding spike loads."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from vault_spikes import __main__ as cli
from vault_spikes import adapters, corpus, spike_ocr
from vault_spikes import manifest as m
from vault_spikes.errors import OwnValueError

REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLIC = REPO_ROOT / "demo" / "corpus"
PRIVATE = "Patient-Diagnosis-Confidential"


def fake_pdf_pages(path: Path) -> list[str] | None:
    """A stand-in for pypdfium2: a file holding TEXT has text on its first page, SCAN has pages
    without text, and anything else cannot be read."""
    data = path.read_bytes()
    if b"TEXT" in data:
        return ["Some words on the first page", ""]
    if b"SCAN" in data:
        return ["", "  \n"]
    return None


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def link_or_skip(case: unittest.TestCase, link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        case.skipTest("cannot create symbolic links here")


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / PRIVATE
        self.root.mkdir()

    def write(self, relative: str, data: bytes = b"text") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def sample(self) -> None:
        """A folder with each case: nested folders, every kind, a copy, a scan and a corrupt PDF,
        an unsupported file, hidden files and an old manifest at the root."""
        self.write("notes.txt", b"plain notes")
        self.write("Clients/Patient-Name/diagnosis.md", b"# heading")
        self.write("Clients/Patient-Name/hours.csv", b"a,b\n1,2\n")
        self.write("Clients/Patient-Name/copy of notes.txt", b"plain notes")
        self.write("letters/letter.docx", b"PK not really")
        self.write("photos/receipt.JPG", b"\xff\xd8")
        self.write("photos/screen.png", b"\x89PNG")
        self.write("pdfs/text.pdf", b"%PDF TEXT")
        self.write("pdfs/scan.pdf", b"%PDF SCAN")
        self.write("pdfs/scan-copy.pdf", b"%PDF SCAN")
        self.write("pdfs/broken.pdf", b"%PDF ???")
        self.write("program.exe", b"MZ")
        self.write("data.json", b"{}")
        self.write(".hidden-notes.txt", b"hidden")
        self.write(".git/config.txt", b"hidden folder")
        self.write("letters/~$letter.docx", b"lock file")
        self.write("manifest.json", b"{}")

    def build(self) -> tuple[dict[str, Any], m.Tally]:
        return m.build_manifest(self.root, fake_pdf_pages)


class BuildManifestTest(Folder):
    def test_lists_every_known_kind_below_the_folder_and_counts_the_rest(self) -> None:
        self.sample()
        manifest, tally = self.build()
        rows = {row["path"]: row for row in manifest["documents"]}
        self.assertEqual(
            list(rows),
            sorted(rows),
            "listed in sorted path order",
        )
        self.assertEqual(
            {path: (row["kind"], row["pages"], row["needs_ocr"]) for path, row in rows.items()},
            {
                "Clients/Patient-Name/copy of notes.txt": ("txt", None, False),
                "Clients/Patient-Name/diagnosis.md": ("md", None, False),
                "Clients/Patient-Name/hours.csv": ("csv", None, False),
                "letters/letter.docx": ("docx", None, False),
                "notes.txt": ("txt", None, False),
                "pdfs/scan-copy.pdf": ("pdf_scan", 2, True),
                "pdfs/scan.pdf": ("pdf_scan", 2, True),
                "pdfs/text.pdf": ("pdf_text", 2, False),
                "photos/receipt.JPG": ("jpg", None, True),
                "photos/screen.png": ("png", None, True),
            },
        )
        # The first of a set of copies in path order is the original.
        self.assertEqual(
            rows["notes.txt"]["exact_duplicate_of"], "Clients/Patient-Name/copy of notes.txt"
        )
        self.assertEqual(rows["pdfs/scan.pdf"]["exact_duplicate_of"], "pdfs/scan-copy.pdf")
        self.assertNotIn("exact_duplicate_of", rows["pdfs/scan-copy.pdf"])
        self.assertEqual(rows["notes.txt"]["bytes"], len(b"plain notes"))
        self.assertEqual(rows["notes.txt"]["sha256"], corpus.file_digest(self.root / "notes.txt"))
        self.assertEqual(tally.listed, 10)
        self.assertEqual(tally.duplicates, 2)
        self.assertEqual(tally.unsupported, 2)  # the program and the JSON file
        self.assertEqual(tally.unreadable, 1)  # the PDF that cannot be opened
        self.assertEqual(tally.hidden, 3)  # a dot file, a dot folder, an Office lock file
        self.assertEqual(tally.by_kind["pdf_scan"], 2)
        self.assertEqual(sum(tally.by_kind.values()), tally.listed)

    def test_a_file_that_cannot_be_read_is_counted_not_listed(self) -> None:
        self.write("a.txt")
        self.write("b.txt", b"other")
        real = m._digest

        def held(path: Path) -> tuple[str, int]:
            if path.name == "b.txt":
                raise PermissionError(PRIVATE)  # another program holds it
            return real(path)

        with mock.patch.object(m, "_digest", side_effect=held):
            manifest, tally = self.build()
        self.assertEqual([row["path"] for row in manifest["documents"]], ["a.txt"])
        self.assertEqual(tally.unreadable, 1)

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "needs file permissions")
    def test_a_file_without_read_permission_is_counted(self) -> None:
        self.write("a.txt")
        locked = self.write("locked.txt", b"locked")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        _, tally = self.build()
        self.assertEqual((tally.listed, tally.unreadable), (1, 1))

    @unittest.skipIf(sys.platform == "win32" or os.geteuid() == 0, "needs file permissions")
    def test_a_subfolder_that_cannot_be_listed_is_counted(self) -> None:
        self.write("a.txt")
        self.write("closed/b.txt")
        (self.root / "closed").chmod(0)
        self.addCleanup((self.root / "closed").chmod, 0o700)
        _, tally = self.build()
        self.assertEqual((tally.listed, tally.folders_unreadable), (1, 1))

    def test_links_are_counted_and_never_followed(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside the folder", encoding="utf-8")
        self.write("a.txt")
        link_or_skip(self, self.root / "linked-folder", outside)
        link_or_skip(self, self.root / "linked-file.txt", outside / "secret.txt")
        link_or_skip(self, self.root / "dangling.txt", outside / "missing.txt")
        (self.root / "loop").mkdir()
        link_or_skip(self, self.root / "loop" / "back", self.root)
        manifest, tally = self.build()
        self.assertEqual([row["path"] for row in manifest["documents"]], ["a.txt"])
        self.assertEqual(tally.links, 4)

    @unittest.skipUnless(sys.platform == "win32", "junctions are a Windows feature")
    def test_a_junction_is_counted_and_never_entered(self) -> None:
        import _winapi

        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside the folder", encoding="utf-8")
        self.write("a.txt")
        _winapi.CreateJunction(str(outside), str(self.root / "junction"))  # type: ignore[attr-defined,unused-ignore]
        manifest, tally = self.build()
        self.assertEqual([row["path"] for row in manifest["documents"]], ["a.txt"])
        self.assertEqual(tally.links, 1)

    @unittest.skipUnless(sys.platform == "win32", "file attributes are a Windows feature")
    def test_a_file_with_the_hidden_or_system_attribute_is_counted(self) -> None:
        import ctypes

        self.write("a.txt")
        for name, attribute in (("hidden.txt", 0x2), ("desktop.txt", 0x4)):
            path = self.write(name)
            set_attributes = ctypes.windll.kernel32.SetFileAttributesW  # type: ignore[attr-defined,unused-ignore]
            self.assertTrue(set_attributes(str(path), attribute))
        _, tally = self.build()
        self.assertEqual((tally.listed, tally.hidden), (1, 2))

    def test_names_the_loader_would_read_as_another_file_keep_only_the_first(self) -> None:
        self.write("Report.txt", b"one")
        self.write("report.txt", b"two")
        if len(list(self.root.iterdir())) != 2:
            self.skipTest("this file system ignores case")
        manifest, tally = self.build()
        self.assertEqual([row["path"] for row in manifest["documents"]], ["Report.txt"])
        self.assertEqual(tally.names_unusable, 1)

    @unittest.skipIf(sys.platform == "win32", "Windows names cannot hold these characters")
    def test_names_json_or_the_loader_cannot_hold_are_counted(self) -> None:
        self.write("a.txt")
        self.write("back\\slash.txt")
        self.write("line\nbreak.txt")
        try:
            (self.root / os.fsdecode(b"not-utf8-\xff.txt")).write_bytes(b"x")
        except (OSError, UnicodeError):
            self.skipTest("this file system refuses a name that is not UTF-8")
        manifest, tally = self.build()
        self.assertEqual([row["path"] for row in manifest["documents"]], ["a.txt"])
        self.assertEqual(tally.names_unusable, 3)

    def test_a_missing_folder_is_refused_without_naming_it(self) -> None:
        for missing in (self.root / "missing", self.write("a-file.txt")):
            with self.subTest(missing.name), self.assertRaises(OwnValueError) as caught:
                m.build_manifest(missing, fake_pdf_pages)
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_every_count_reaches_the_summary(self) -> None:
        tally = m.Tally()
        counts = [f.name for f in dataclasses.fields(tally) if f.name != "by_kind"]
        for number, name in enumerate(counts, start=101):
            setattr(tally, name, number)
        tally.by_kind = {kind: number for number, kind in enumerate(m.KINDS, start=201)}
        text = "\n".join(tally.lines())
        for number in [*range(101, 101 + len(counts)), *range(201, 201 + len(m.KINDS))]:
            self.assertIn(f": {number}\n", text + "\n")


class KindsTest(unittest.TestCase):
    """The table of kinds follows the code that reads each kind."""

    def test_every_suffix_the_corpus_reader_reads_is_listed(self) -> None:
        for suffix in (*corpus.TEXT_SUFFIXES, ".docx"):
            self.assertIn(suffix, m.KIND_BY_SUFFIX)
            self.assertNotIn(m.KIND_BY_SUFFIX[suffix], m.IMAGE_KINDS)

    def test_every_image_the_ocr_spike_reads_is_listed_as_needing_ocr(self) -> None:
        for suffix in spike_ocr.IMAGE_SUFFIXES:
            self.assertIn(m.KIND_BY_SUFFIX.get(suffix), m.IMAGE_KINDS)

    def test_every_kind_written_is_in_the_summary_and_the_public_kinds_are_known(self) -> None:
        self.assertEqual(set(m.KINDS), {*m.KIND_BY_SUFFIX.values(), "pdf_text", "pdf_scan"})
        public = json.loads((PUBLIC / "manifest.json").read_text(encoding="utf-8"))
        self.assertLessEqual({row["kind"] for row in public["documents"]}, set(m.KINDS))


class WriteManifestTest(Folder):
    def test_the_written_manifest_loads_and_builds_a_corpus(self) -> None:
        self.sample()
        (self.root / "manifest.json").unlink()
        manifest, _ = self.build()
        m.write_manifest(self.root, manifest, force=False)
        documents = corpus.load_manifest(self.root / "manifest.json")
        self.assertEqual(len(documents), 10)
        built = corpus.build_corpus(self.root, documents, fake_pdf_pages)
        # The DOCX is not a real one and the scans and images need OCR; the copies are compared
        # byte for byte with their originals by the corpus builder.
        self.assertIn("notes.txt", built.readable)
        self.assertIn("Clients/Patient-Name/copy of notes.txt", built.readable)
        self.assertIn("pdfs/text.pdf", built.readable)
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.startswith(".manifest-")], []
        )

    def test_an_existing_manifest_is_kept_unless_forced(self) -> None:
        self.write("a.txt")
        old = self.write("manifest.json", b"old")
        manifest, _ = self.build()
        with self.assertRaises(OwnValueError):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(old.read_bytes(), b"old")
        m.write_manifest(self.root, manifest, force=True)
        self.assertEqual(len(corpus.load_manifest(old)), 1)

    def test_a_manifest_that_is_not_a_regular_file_is_never_replaced(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        (self.root / "manifest.json").mkdir()
        with self.assertRaises(OwnValueError):
            m.write_manifest(self.root, manifest, force=True)
        (self.root / "manifest.json").rmdir()
        outside = Path(self.temporary.name) / "elsewhere.json"
        outside.write_bytes(b"keep")
        link_or_skip(self, self.root / "manifest.json", outside)
        with self.assertRaises(OwnValueError):
            m.write_manifest(self.root, manifest, force=True)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_a_manifest_that_appears_during_the_run_is_not_replaced(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        real = corpus.load_manifest

        def meanwhile(path: Path) -> list[corpus.Document]:
            (self.root / "manifest.json").write_bytes(b"written by someone else")
            return real(path)

        with (
            mock.patch("vault_spikes.manifest.load_manifest", side_effect=meanwhile),
            self.assertRaises(OwnValueError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual((self.root / "manifest.json").read_bytes(), b"written by someone else")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "manifest.json"])

    def test_without_hard_links_the_manifest_is_still_written(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        with mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(len(corpus.load_manifest(self.root / "manifest.json")), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "manifest.json"])

    def appears_meanwhile(self) -> mock._patch[Any]:
        """Patch the read-back so that another process writes ``manifest.json`` after
        :func:`check_target` has passed and before the manifest is put in place."""
        real = corpus.load_manifest

        def meanwhile(path: Path) -> list[corpus.Document]:
            (self.root / "manifest.json").write_bytes(b"written by someone else")
            return real(path)

        return mock.patch("vault_spikes.manifest.load_manifest", side_effect=meanwhile)

    def assert_kept(self) -> None:
        self.assertEqual((self.root / "manifest.json").read_bytes(), b"written by someone else")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "manifest.json"])

    def test_without_hard_links_a_manifest_that_appears_is_not_replaced(self) -> None:
        # The fallback this platform takes (a rename on Windows, an exclusive create elsewhere).
        self.write("a.txt")
        manifest, _ = self.build()
        with (
            self.appears_meanwhile(),
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            self.assertRaises(OwnValueError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assert_kept()

    def test_the_exclusive_copy_never_replaces_a_manifest_that_appears(self) -> None:
        # The fallback for a system other than Windows, run on every platform.
        self.write("a.txt")
        manifest, _ = self.build()
        with (
            self.appears_meanwhile(),
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(os, "name", "posix"),
            self.assertRaises(OwnValueError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assert_kept()

    def test_the_exclusive_copy_writes_the_whole_manifest(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        with (
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(os, "name", "posix"),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(len(corpus.load_manifest(self.root / "manifest.json")), 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt", "manifest.json"])

    def test_the_exclusive_copy_never_follows_a_link_that_appears(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        outside = Path(tempfile.mkdtemp()) / "outside.json"
        self.addCleanup(shutil.rmtree, outside.parent)
        outside.write_bytes(b"keep")
        real = corpus.load_manifest

        def meanwhile(path: Path) -> list[corpus.Document]:
            link_or_skip(self, self.root / "manifest.json", outside)
            return real(path)

        with (
            mock.patch("vault_spikes.manifest.load_manifest", side_effect=meanwhile),
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(os, "name", "posix"),
            self.assertRaises(OwnValueError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_a_failed_exclusive_copy_leaves_no_partial_manifest(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        with (
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(os, "name", "posix"),
            mock.patch.object(os, "fsync", side_effect=[None, OSError("disk full")]),
            self.assertRaises(OSError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt"])

    def test_the_temporary_name_taken_by_another_file_is_not_removed(self) -> None:
        # The cleanup of the temporary file runs after publishing; if another process has put
        # its own file at that name meanwhile, that file stays.
        self.write("a.txt")
        manifest, _ = self.build()
        taken: list[Path] = []

        def publish_then_taken(temporary: Path, target: Path) -> None:
            temporary.replace(target)
            temporary.write_text("someone else's", encoding="utf-8")
            taken.append(temporary)

        with mock.patch.object(m, "_publish_new", side_effect=publish_then_taken):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].read_text(encoding="utf-8"), "someone else's")

    @unittest.skipIf(os.name == "nt", "Windows does not let an open file be removed or moved")
    def test_a_failed_copy_leaves_a_manifest_that_took_the_name_meanwhile(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        target = self.root / "manifest.json"
        real_fsync = os.fsync
        calls: list[int] = []

        def fsync(fd: int) -> None:
            calls.append(fd)
            if len(calls) == 2:  # the copy into the name this run created
                target.replace(self.root / "moved.json")
                target.write_bytes(b"written by someone else")
                raise OSError("disk full")
            real_fsync(fd)

        with (
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(os, "name", "posix"),
            mock.patch.object(os, "fsync", side_effect=fsync),
            self.assertRaises(OSError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(target.read_bytes(), b"written by someone else")

    @unittest.skipUnless(os.name == "nt", "a rename that never replaces is Windows behaviour")
    def test_the_windows_rename_never_replaces_a_manifest_that_appears(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        with (
            self.appears_meanwhile(),
            mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
            mock.patch.object(m, "_copy_exclusive", side_effect=AssertionError("not Windows")),
            self.assertRaises(OwnValueError),
        ):
            m.write_manifest(self.root, manifest, force=False)
        self.assert_kept()

    def test_publishing_without_force_never_uses_an_operation_that_replaces(self) -> None:
        # A check followed by a replace leaves a window in which another process's manifest is
        # overwritten, so no fallback may use one: ``os.replace`` never, and ``os.rename`` only
        # on Windows, where it refuses an existing name (elsewhere it replaces).
        for name in ["posix"] + (["nt"] if os.name == "nt" else []):
            with self.subTest(fallback=name):
                for leftover in self.root.iterdir():
                    leftover.unlink()
                self.write("a.txt")
                manifest, _ = self.build()
                replacing = mock.patch.object(
                    os, "rename", side_effect=AssertionError("rename used off Windows")
                )
                with (
                    mock.patch.object(os, "link", side_effect=OSError("no hard links on FAT")),
                    mock.patch.object(os, "replace", side_effect=AssertionError("replace used")),
                    mock.patch.object(os, "name", name),
                    replacing if name != "nt" else contextlib.nullcontext(),
                ):
                    m.write_manifest(self.root, manifest, force=False)
                self.assertEqual(len(corpus.load_manifest(self.root / "manifest.json")), 1)

    def test_a_manifest_that_does_not_read_back_is_not_put_in_place(self) -> None:
        self.write("a.txt")
        manifest, _ = self.build()
        manifest["documents"][0]["path"] = "missing.txt"
        with self.assertRaises(corpus.SpikeInputError):
            m.write_manifest(self.root, manifest, force=False)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["a.txt"])

    def test_names_outside_ascii_are_written_as_utf8_and_read_back(self) -> None:
        name = "résumé über 履歴.txt"  # built from code points
        self.write(name)
        manifest, _ = self.build()
        m.write_manifest(self.root, manifest, force=False)
        data = (self.root / "manifest.json").read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn(name.encode("utf-8"), data)
        self.assertEqual(
            [d.path for d in corpus.load_manifest(self.root / "manifest.json")], [name]
        )


@unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "pypdfium2 is not installed")
class PublicCorpusTest(unittest.TestCase):
    """With the real PDF reader, the public corpus's files give the public manifest back."""

    def test_the_public_files_give_the_public_manifest(self) -> None:
        public = json.loads((PUBLIC / "manifest.json").read_text(encoding="utf-8"))["documents"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for row in public:  # only the listed files: anything else in the folder is not a test
                (root / row["path"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(PUBLIC / row["path"], root / row["path"])
            manifest, tally = m.build_manifest(root, adapters.pdf_pages_for_corpus)
        made = {row["path"]: row for row in manifest["documents"]}
        fields = ("kind", "bytes", "sha256", "pages", "needs_ocr")
        self.assertEqual(
            {path: tuple(row[f] for f in fields) for path, row in made.items()},
            {row["path"]: tuple(row[f] for f in fields) for row in public},
        )
        self.assertEqual(tally.listed, len(public))

        def groups(rows: list[dict[str, object]]) -> set[frozenset[object]]:
            """Sets of copies: which file is the original is a choice, the copies are a fact."""
            found: dict[object, set[object]] = {}
            for row in rows:
                original = row.get("exact_duplicate_of") or row["path"]
                found.setdefault(original, {original}).add(row["path"])
            return {frozenset(group) for group in found.values() if len(group) > 1}

        self.assertEqual(groups(list(made.values())), groups(public))


class CommandTest(Folder):
    def setUp(self) -> None:
        super().setUp()
        patches = (
            mock.patch.dict(sys.modules, {"pypdfium2": mock.MagicMock()}),
            mock.patch.object(adapters, "pdf_pages_for_corpus", fake_pdf_pages),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def assert_private(self, *outputs: str) -> None:
        for output in outputs:
            for private in (PRIVATE, "Patient-Name", "diagnosis", "copy of notes", "receipt"):
                self.assertNotIn(private, output)
            self.assertNotIn(self.temporary.name, output)

    def test_prints_counts_only_and_writes_a_manifest_that_loads(self) -> None:
        self.sample()
        (self.root / "manifest.json").unlink()
        code, out, err = run("make-manifest", "--corpus", str(self.root))
        self.assertEqual((code, err), (0, ""))
        self.assert_private(out)
        self.assertIn("documents listed: 10\n", out)
        self.assertIn("  pdf_scan: 2\n", out)
        self.assertIn("exact duplicates marked: 2\n", out)
        self.assertIn("skipped, not a kind the spikes read: 2\n", out)
        self.assertIn("skipped, could not be read: 1\n", out)
        self.assertIn("manifest: manifest.json written in the --corpus folder", out)
        self.assertEqual(len(corpus.load_manifest(self.root / "manifest.json")), 10)

    def test_an_existing_manifest_stops_the_command_before_the_folder_is_read(self) -> None:
        self.sample()
        with mock.patch.object(cli, "build_manifest") as build:
            code, out, err = run("make-manifest", "--corpus", str(self.root))
        build.assert_not_called()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("pass --force to replace it", err)
        self.assert_private(err)
        self.assertEqual((self.root / "manifest.json").read_bytes(), b"{}")
        code, out, err = run("make-manifest", "--corpus", str(self.root), "--force")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(corpus.load_manifest(self.root / "manifest.json")), 10)

    def test_without_pypdfium2_nothing_is_read_or_written(self) -> None:
        self.write("a.pdf", b"%PDF TEXT")
        with (
            mock.patch.dict(sys.modules, {"pypdfium2": None}),
            mock.patch.object(cli, "build_manifest") as build,
        ):
            code, out, err = run("make-manifest", "--corpus", str(self.root))
        build.assert_not_called()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("pypdfium2 is not installed", err)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_a_folder_with_nothing_to_list_writes_nothing(self) -> None:
        self.write("program.exe", b"MZ")
        code, out, err = run("make-manifest", "--corpus", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("documents listed: 0\n", out)
        self.assertIn("no manifest was written", err)
        self.assertFalse((self.root / "manifest.json").exists())

    def test_a_missing_folder_or_a_failure_names_no_path(self) -> None:
        code, out, err = run("make-manifest", "--corpus", str(self.root / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)
        self.assert_private(out, err)
        self.write("a.txt")
        with mock.patch.object(cli, "write_manifest", side_effect=OSError(str(self.root))):
            code, out, err = run("make-manifest", "--corpus", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("OSError", err)
        self.assert_private(out, err)

    def test_a_malformed_command_line_prints_fixed_text(self) -> None:
        folder = str(self.root)
        for argv in (
            ["make-manifest", "--corpus", folder, folder],  # an extra word
            ["make-manifest", folder],  # the folder without its option
            ["make-manifest", "--corpus"],  # a missing value
            ["make-manifest", "--corpus", folder, f"--{PRIVATE}"],  # an unknown option
            ["make-manifest", "--corpus", folder, "--force=yes"],  # a value for a flag
            ["make-manifest", "--corp", folder, "--forc", folder],  # abbreviations and a word
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                self.subTest(argv[1:]),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                cli.main(argv)
            self.assertEqual(caught.exception.code, 2)
            self.assert_private(out.getvalue(), err.getvalue())
            self.assertIn("the command line is not valid", err.getvalue())


if __name__ == "__main__":
    unittest.main()

"""Tests for scripts/check_repo_safety.py (standard library only)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_repo_safety as safety  # noqa: E402


class PathRulesTest(unittest.TestCase):
    def assertBlocked(self, path: str, fragment: str) -> None:
        reasons = safety.path_problems(path)
        self.assertTrue(
            any(fragment in reason for reason in reasons), f"{path}: {reasons} lacks {fragment!r}"
        )

    def assertAllowed(self, path: str) -> None:
        self.assertEqual(safety.path_problems(path), [], path)

    def test_databases(self) -> None:
        for path in ("vault.db", "data/index.sqlite3", "x/vault.db-wal", "a.sqlite-shm"):
            self.assertBlocked(path, "database")

    def test_env_and_config(self) -> None:
        self.assertBlocked(".env", "environment file")
        self.assertBlocked(".env.production", "environment file")
        self.assertBlocked("config.toml", "local configuration")
        self.assertBlocked("settings.local.toml", "local configuration")
        for path in (".env.example", "config.example.toml", "config.demo.toml"):
            self.assertAllowed(path)

    def test_keys(self) -> None:
        for path in (
            "server.pem",
            "a/b.key",
            "id_rsa",
            "id_ed25519",
            "credentials.ppk",
            "signing.p8",
            "store.jks",
            "passwords.kdbx",
        ):
            self.assertBlocked(path, "private key")
        self.assertAllowed("id_ed25519.pub")

    def test_model_weights(self) -> None:
        for path in ("m.onnx", "m.safetensors", "models/q.gguf"):
            self.assertBlocked(path, "model weights")

    def test_private_folders(self) -> None:
        self.assertBlocked("blobs/sha256/aa/bb/x.txt", "private data folder")
        self.assertBlocked("logs/app.txt", "private data folder")
        for path in (
            "vault/extracted.txt",
            "vaults/alice/chunks.json",
            "AIVault/notes.txt",
            "models/tokenizer.json",
            "secrets/token.txt",
            "backend/secrets/token.txt",
            ".cache/huggingface/x.json",
        ):
            self.assertBlocked(path, "private data folder")
        self.assertBlocked("run.log", "log file")
        self.assertBlocked("eval/private/questions.yaml", "private evaluation")
        self.assertAllowed("eval/public/questions.yaml")

    def test_documents_only_in_fixture_folders(self) -> None:
        self.assertBlocked("notes/taxes.pdf", "document outside")
        self.assertBlocked("Report.PDF", "document outside")
        self.assertBlocked("fixtures/a.pdf", "document outside")
        self.assertAllowed("tests/fixtures/a.pdf")
        self.assertAllowed("backend/tests/unit/fixtures/a.docx")
        self.assertAllowed("demo/corpus/syllabus.pdf")
        self.assertAllowed("demo\\corpus\\grades.csv")
        for path in ("notes/book.epub", "scans/letter.djvu", "a/b.xlsm", "x/y.pages"):
            self.assertBlocked(path, "document outside")
        self.assertAllowed("tests/fixtures/book.epub")

    def test_archives_only_in_fixture_folders(self) -> None:
        for path in ("notes.zip", "a/b.7z", "x/backup.tar.gz", "scans.rar", "disk.iso"):
            self.assertBlocked(path, "archive")
        for path in ("exports-from-vault.bak", "db/dump.old", "a.sql.dump"):
            self.assertBlocked(path, "backup or dump")
        self.assertAllowed("tests/fixtures/corrupt.zip")
        self.assertAllowed("demo/corpus/bundle.zip")

    def test_images(self) -> None:
        self.assertBlocked("photo.jpg", "image outside")
        for path in (
            "docs/images/arch.png",
            "frontend/public/logo.png",
            "tests/fixtures/scan.png",
            "demo/corpus/receipt.jpg",
        ):
            self.assertAllowed(path)

    def test_ordinary_files_allowed(self) -> None:
        for path in (
            "README.md",
            "backend/app/main.py",
            "frontend/src/App.tsx",
            "backend/src/vault/main.py",
            "backend/src/vault/storage/blob_store.py",
            "docs/models.md",
        ):
            self.assertAllowed(path)


ZIP = b"PK\x03\x04" + b"\0" * 28
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 24
BMP = b"BM" + b"\0" * 12 + (40).to_bytes(4, "little") + b"\0" * 20
TAR = b"\0" * 257 + b"ustar\x0000" + b"\0" * 20
PALM_BOOK = b"\0" * 60 + b"BOOKMOBI" + b"\0" * 20
HEIF = b"\0\0\0\x18ftyp"
SIGNATURE_SAMPLES = {
    ".7z": b"7z\xbc\xaf\x27\x1c" + b"\0" * 20,
    ".avif": HEIF + b"avif" + b"\0" * 20,
    ".azw": PALM_BOOK,
    ".azw3": PALM_BOOK,
    ".bmp": BMP,
    ".bz2": b"BZh91AY&SY" + b"\0" * 20,
    ".cab": b"MSCF\0\0\0\0" + b"\0" * 20,
    ".djvu": b"AT&TFORM" + b"\0" * 20,
    ".doc": OLE,
    ".docm": ZIP,
    ".docx": ZIP,
    ".dotx": ZIP,
    ".epub": ZIP,
    ".gif": b"GIF89a" + b"\0" * 20,
    ".gz": b"\x1f\x8b\x08" + b"\0" * 20,
    ".heic": HEIF + b"heic" + b"\0" * 20,
    ".jpeg": b"\xff\xd8\xff\xe0" + b"\0" * 20,
    ".jpg": b"\xff\xd8\xff\xe0" + b"\0" * 20,
    ".mobi": PALM_BOOK,
    ".msg": OLE,
    ".numbers": ZIP,
    ".odp": ZIP,
    ".ods": ZIP,
    ".odt": ZIP,
    ".pages": ZIP,
    ".pdf": b"%PDF-1.7\n" + b"\0" * 20,
    ".png": b"\x89PNG\r\n\x1a\n" + b"\0" * 20,
    ".potx": ZIP,
    ".ppt": OLE,
    ".pptx": ZIP,
    ".rar": b"Rar!\x1a\x07\x00" + b"\0" * 20,
    ".rtf": b"{\\rtf1\\ansi",
    ".tar": TAR,
    ".tgz": b"\x1f\x8b\x08" + b"\0" * 20,
    ".tif": b"II*\x00" + b"\0" * 20,
    ".tiff": b"MM\x00*" + b"\0" * 20,
    ".webp": b"RIFF\0\0\0\0WEBPVP8 ",
    ".xls": OLE,
    ".xlsm": ZIP,
    ".xlsx": ZIP,
    ".xps": ZIP,
    ".xz": b"\xfd7zXZ\x00" + b"\0" * 20,
    ".zip": ZIP,
    ".zst": b"\x28\xb5\x2f\xfd" + b"\0" * 20,
}
TEXT_OR_DEEP_SIGNATURE = frozenset({".csv", ".eml", ".iso", ".tsv"})
"""Formats with no signature in the first bytes: plain text, or an offset past what is read."""


class ContentSignatureTest(unittest.TestCase):
    def test_every_blocked_suffix_has_a_signature_check(self) -> None:
        suffixes = {
            *safety.DOCUMENT_SUFFIXES,
            *safety.IMAGE_SUFFIXES,
            *safety.ARCHIVE_SUFFIXES,
        }
        self.assertEqual(suffixes - set(SIGNATURE_SAMPLES), TEXT_OR_DEEP_SIGNATURE)

    def test_samples_are_detected_under_any_name(self) -> None:
        for suffix, sample in SIGNATURE_SAMPLES.items():
            self.assertIsNotNone(safety.content_problem(sample), suffix)
            reasons = safety.reasons_for("notes/innocent.txt", len(sample), sample)
            self.assertTrue(reasons, suffix)

    def test_samples_are_allowed_in_fixture_folders(self) -> None:
        for suffix, sample in SIGNATURE_SAMPLES.items():
            reasons = safety.reasons_for("tests/fixtures/x.txt", len(sample), sample)
            self.assertEqual(reasons, [], suffix)

    def test_plain_text_is_not_mistaken_for_a_signature(self) -> None:
        for text in (b"BM is a prefix", b"%PDF is mentioned", b"MSCF is a word", b"PK ranking"):
            self.assertIsNone(safety.content_problem(text), text)


class SizeRulesTest(unittest.TestCase):
    def test_limits(self) -> None:
        mib = safety.MIB
        self.assertIsNone(safety.size_problem("a.py", mib))
        self.assertIn("over the 1 MiB", safety.size_problem("a.py", mib + 1) or "")
        self.assertIsNone(safety.size_problem("tests/fixtures/a.pdf", 5 * mib))
        self.assertIsNotNone(safety.size_problem("tests/fixtures/a.pdf", 5 * mib + 1))
        self.assertIsNone(safety.size_problem("frontend/package-lock.json", 3 * mib))

    def test_sqlite_content(self) -> None:
        self.assertIsNotNone(safety.content_problem(safety.SQLITE_HEADER))
        self.assertIsNone(safety.content_problem(b"hello"))

    def test_archive_content(self) -> None:
        for head in (
            b"PK\x03\x04" + b"\0" * 12,
            b"\x1f\x8b\x08" + b"\0" * 13,
            b"7z\xbc\xaf\x27\x1c" + b"\0" * 10,
            b"Rar!\x1a\x07\x00" + b"\0" * 9,
            b"BZh91AY&SY" + b"\0" * 6,
            b"MSCF\0\0\0\0" + b"\0" * 8,
        ):
            self.assertIn("archive", safety.content_problem(head) or "", head)
            self.assertIsNone(safety.content_problem(head, frozenset({"archive"})), head)
        self.assertIsNone(safety.content_problem(b"BZh is not bzip2"))

    def test_document_and_image_content(self) -> None:
        documents = (
            b"%PDF-1.7\n%\xe2\xe3",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 8,
            b"{\\rtf1\\ansi",
        )
        images = (
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8,
            b"\xff\xd8\xff\xe0" + b"\0" * 12,
            b"GIF89a" + b"\0" * 10,
        )
        webp = b"RIFF\0\0\0\0WEBPVP8 "
        for head in documents:
            self.assertIn("document", safety.content_problem(head) or "", head)
            self.assertIsNone(safety.content_problem(head, frozenset({"document"})), head)
        for head in (*images, webp):
            self.assertIn("image", safety.content_problem(head) or "", head)
            self.assertIsNone(safety.content_problem(head, frozenset({"image"})), head)
        self.assertIsNone(safety.content_problem(b"%PDF is mentioned in this text"))

    def testallowed_content_by_location(self) -> None:
        self.assertEqual(len(safety.allowed_content("tests/fixtures/a.txt")), 3)
        self.assertEqual(safety.allowed_content("docs/images/a.txt"), frozenset({"image"}))
        self.assertEqual(safety.allowed_content("notes/a.txt"), frozenset())


class GitIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def commit(self, name: str, data: bytes) -> None:
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.git("add", "-f", name)
        self.git("commit", "-q", "-m", f"add {name}")

    def run_check(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "check_repo_safety.py"), *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )

    def test_clean_repository_passes(self) -> None:
        self.commit("README.md", b"hello\n")
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tracked_database_fails(self) -> None:
        self.commit("vault.db", b"x")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("vault.db", result.stderr)

    def test_force_added_vault_folder_files_fail(self) -> None:
        self.commit("vault/extracted.txt", b"text taken from a private document\n")
        self.commit("backend/src/vault/main.py", b"print('code')\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("vault/extracted.txt", result.stderr)
        self.assertNotIn("backend/src/vault/main.py", result.stderr)

    def test_disguised_pdf_caught_in_index_and_history(self) -> None:
        self.commit("notes.txt", b"%PDF-1.7\nprivate scanned letter\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("document (PDF, Office or RTF) detected", result.stderr)
        self.git("rm", "-q", "notes.txt")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.run_check().returncode, 0)
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Already in git history", result.stderr)

    def test_disguised_cab_and_image_caught(self) -> None:
        self.commit("notes.txt", b"MSCF\0\0\0\0" + b"\0" * 100)
        self.commit("photo.txt", b"\x89PNG\r\n\x1a\n" + b"\0" * 100)
        self.commit("docs/images/diagram.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 100)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("archive detected", result.stderr)
        self.assertIn("image detected", result.stderr)
        self.assertNotIn("diagram.png", result.stderr)

    def test_disguised_tar_bmp_and_backup_caught_in_index_and_history(self) -> None:
        self.commit("notes.txt", TAR)
        self.commit("scan.txt", BMP)
        self.commit("exports-from-vault.bak", b"text taken from a private document\n")
        for arguments in ((), ("--history",)):
            result = self.run_check(*arguments)
            self.assertEqual(result.returncode, 1)
            for expected in ("notes.txt", "scan.txt", "exports-from-vault.bak"):
                self.assertIn(expected, result.stderr)
        for name in ("notes.txt", "scan.txt", "exports-from-vault.bak"):
            self.git("rm", "-q", name)
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.run_check().returncode, 0)
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Already in git history", result.stderr)
        for expected in ("archive detected", "image detected", "backup or dump file"):
            self.assertIn(expected, result.stderr)

    def test_archive_content_detected_under_innocent_name(self) -> None:
        self.commit("notes.txt", b"PK\x03\x04" + b"\0" * 100)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("archive detected", result.stderr)
        self.commit("tests/fixtures/sample.docx", b"PK\x03\x04" + b"\0" * 100)
        self.assertNotIn("sample.docx", self.run_check().stderr)

    def test_sqlite_content_detected_under_innocent_name(self) -> None:
        self.commit("notes.txt", safety.SQLITE_HEADER + b"\0" * 100)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite database detected", result.stderr)

    def test_deleted_file_still_caught_in_history(self) -> None:
        self.commit("vault.db", b"x")
        self.git("rm", "-q", "vault.db")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.run_check().returncode, 0)
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Already in git history", result.stderr)

    def test_deleted_sqlite_content_under_innocent_name_caught_in_history(self) -> None:
        self.commit("notes.txt", safety.SQLITE_HEADER + b"\0" * 100)
        self.git("rm", "-q", "notes.txt")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.run_check().returncode, 0)
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite database detected", result.stderr)

    def test_renamed_file_caught_under_its_old_name_in_history(self) -> None:
        self.commit("taxes.pdf", b"%PDF-1.7 private\n")
        self.git("mv", "taxes.pdf", "README.md")
        self.git("commit", "-q", "-m", "rename")
        self.git("rm", "-q", "README.md")
        self.git("commit", "-q", "-m", "remove")
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("taxes.pdf", result.stderr)
        self.assertIn("document outside", result.stderr)

    def test_history_covers_merged_branches(self) -> None:
        self.commit("README.md", b"hello\n")
        self.git("checkout", "-q", "-b", "side")
        self.commit("vault.db", b"x")
        self.git("rm", "-q", "vault.db")
        self.git("commit", "-q", "-m", "remove")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side")
        self.git("branch", "-q", "-D", "side")
        result = self.run_check("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vault.db", result.stderr)

    def stage_database_then_overwrite_with_text(self) -> None:
        target = self.repo / "notes.txt"
        target.write_bytes(safety.SQLITE_HEADER + b"\0" * 100)
        self.git("add", "notes.txt")
        target.write_text("harmless\n")

    def test_staged_bytes_are_checked_not_the_working_copy(self) -> None:
        self.stage_database_then_overwrite_with_text()
        self.assertEqual(self.run_check("notes.txt").returncode, 0)  # the copy on disk is harmless
        result = self.run_check("--staged", "notes.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite database detected", result.stderr)

    def test_all_tracked_mode_checks_the_index(self) -> None:
        self.stage_database_then_overwrite_with_text()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SQLite database detected", result.stderr)

    def test_staged_mode_falls_back_to_disk_for_unstaged_files(self) -> None:
        self.commit("README.md", b"hello\n")
        (self.repo / "big.txt").write_bytes(b"a" * (safety.MIB + 1))
        self.assertEqual(self.run_check("--staged", "big.txt").returncode, 1)
        self.assertEqual(self.run_check("--staged", "README.md").returncode, 0)

    def test_explicit_paths_mode(self) -> None:
        self.commit("README.md", b"hello\n")
        (self.repo / "big.txt").write_bytes(b"a" * (safety.MIB + 1))
        result = self.run_check("big.txt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MiB is over", result.stderr)
        self.assertEqual(self.run_check("README.md").returncode, 0)


if __name__ == "__main__":
    unittest.main()

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
        for path in ("server.pem", "a/b.key", "id_rsa", "id_ed25519"):
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

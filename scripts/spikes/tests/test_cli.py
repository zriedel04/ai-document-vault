from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vault_spikes import __main__ as cli
from vault_spikes import adapters
from vault_spikes.report import Environment, Report

KNOWN = cli.KNOWN

PRIVATE_TEXT = "PATIENT-DIAGNOSIS-CONFIDENTIAL"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class FakeRapid:
    name = "rapidocr-ppocrv5"

    def load(self) -> None:
        pass

    def recognise(self, image: Path) -> str:
        return image.read_text(encoding="utf-8")


class CliTest(unittest.TestCase):
    def test_check_lists_the_optional_pieces_with_the_extra_to_install(self) -> None:
        with mock.patch.object(
            cli, "available", return_value={"pypdfium2": False, "tesseract_on_path": False}
        ):
            code, out, _ = run("check")
        self.assertEqual(code, 0)
        self.assertIn("pypdfium2: no   (uv sync --extra pdf)", out)
        self.assertIn("sqlite can load extensions:", out)

    def test_an_ocr_run_writes_a_report_with_numbers_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            (images / "a.png").write_text(f"{PRIVATE_TEXT} alpha", encoding="utf-8")
            (images / "a.txt").write_text(f"{PRIVATE_TEXT} alpha", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                code, out, err = run(
                    "ocr",
                    "--engine",
                    "rapidocr",
                    "--images",
                    str(images),
                    "--out",
                    str(root / "out"),
                )
            self.assertEqual(code, 0, err)
            written = (root / "out" / "ocr-rapidocr-ppocrv5.json").read_text(encoding="utf-8")
            markdown = (root / "out" / "ocr-rapidocr-ppocrv5.md").read_text(encoding="utf-8")
        for text in (written, markdown, out, err):
            self.assertNotIn(PRIVATE_TEXT, text)
        self.assertEqual(json.loads(written)["rows"][0]["metrics"]["cer_mean"], 0.0)

    def test_a_tesseract_report_reads_back_and_merges_with_another_ocr_report(self) -> None:
        # The version line is "tesseract 5.5.0": stored whole it fails the read-back check, and
        # merge, which reads reports from disk, could not combine the two engines.
        version_line = mock.Mock(stdout="tesseract 5.5.0\n leptonica-1.84\n", stderr="")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            (images / "a.png").write_text("alpha", encoding="utf-8")
            (images / "a.txt").write_text("alpha", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                code, _, err = run(
                    "ocr", "--engine", "rapidocr", "--images", str(images), "--out", str(root)
                )
            self.assertEqual(code, 0, err)
            ocr_output = mock.Mock(stdout=b"alpha", stderr=b"")

            def fake_run(command: list[str], **_: object) -> mock.Mock:
                return version_line if "--version" in command else ocr_output

            with (
                mock.patch("shutil.which", return_value="tess"),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                code, _, err = run(
                    "ocr", "--engine", "tesseract", "--images", str(images), "--out", str(root)
                )
            self.assertEqual(code, 0, err)
            stored = json.loads((root / "ocr-tesseract.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["environment"]["packages"]["tesseract"], "5.5.0")
            code, _, err = run(
                "merge",
                str(root / "ocr-rapidocr-ppocrv5.json"),
                str(root / "ocr-tesseract.json"),
                "--out",
                str(root / "merged"),
            )
            self.assertEqual(code, 0, err)
            merged = json.loads((root / "merged" / "ocr.json").read_text(encoding="utf-8"))
        self.assertEqual(len(merged["rows"]), 2)

    def test_only_the_repositorys_own_files_are_called_the_public_dataset(self) -> None:
        corpus = cli.REPO_ROOT / "demo" / "corpus"
        questions = cli.REPO_ROOT / "eval" / "public" / "questions.yaml"
        self.assertTrue(cli.is_public_dataset(corpus, questions))
        self.assertTrue(cli.is_public_dataset(corpus / ".." / "corpus", questions))
        with tempfile.TemporaryDirectory() as folder:
            elsewhere = Path(folder)
            self.assertFalse(cli.is_public_dataset(elsewhere, questions))
            self.assertFalse(cli.is_public_dataset(corpus, elsewhere / "questions.yaml"))
            # A copy that merely has the same names is the owner's data until proven otherwise.
            (elsewhere / "demo" / "corpus").mkdir(parents=True)
            self.assertFalse(cli.is_public_dataset(elsewhere / "demo" / "corpus", questions))

    def test_merge_combines_candidate_reports(self) -> None:
        env = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for name in ("qwen3-embedding-0.6b", "embeddinggemma-300m"):
                report = Report("embedding", *KNOWN["embedding"].text, env)
                report.add(f"{name}-cpu", chunks=1)
                paths.append(str(report.write(root, stem=f"embedding-{name}")[0]))
            code, _, err = run("merge", *paths, "--out", str(root / "merged"))
            self.assertEqual(code, 0, err)
            merged = json.loads((root / "merged" / "embedding.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [r["candidate"] for r in merged["rows"]],
            ["qwen3-embedding-0.6b-cpu", "embeddinggemma-300m-cpu"],
        )

    def test_merge_refuses_a_report_with_text_of_its_own(self) -> None:
        env = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = Report("embedding", "Patient diagnosis", "Q?", "How.", env)
            report.add("qwen3-embedding-0.6b-cpu", chunks=1)
            path = str(report.write(root, stem="embedding-a")[0])
            code, out, err = run("merge", path, "--out", str(root / "merged"))
            self.assertFalse((root / "merged").exists())
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("Patient", err)

    def test_merge_refuses_names_that_only_look_harmless(self) -> None:
        text = "patient-diagnosis-confidential"
        good = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        cases = {
            "candidate": (good, text, {"chunks": 1}),
            "metric": (good, "qwen3-embedding-0.6b-cpu", {"medical_record_number": 1}),
            "package": (
                Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {text: "1.0"}),
                "qwen3-embedding-0.6b-cpu",
                {"chunks": 1},
            ),
        }
        for kind, (env, candidate, metrics) in cases.items():
            with tempfile.TemporaryDirectory() as folder, self.subTest(kind):
                root = Path(folder)
                report = Report("embedding", *KNOWN["embedding"].text, env)
                report.add(candidate, **metrics)
                path = str(report.write(root, stem="embedding-x")[0])
                code, out, err = run("merge", path, "--out", str(root / "merged"))
                self.assertFalse((root / "merged").exists())
            self.assertEqual((code, out), (2, ""))
            self.assertIn(f"{kind} name", err)
            self.assertNotIn("patient", err)
            self.assertNotIn("medical", err)

    def test_fetch_model_downloads_through_the_adapter(self) -> None:
        with mock.patch.object(adapters, "fetch_model") as fetch:
            code, out, err = run("fetch-model", "--candidate", "qwen3-embedding-0.6b")
        self.assertEqual((code, err), (0, ""))
        fetch.assert_called_once_with("qwen3-embedding-0.6b")
        self.assertEqual(out.strip(), "qwen3-embedding-0.6b: downloaded")

    def test_errors_are_one_line_on_stderr_with_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, out, err = run("pdf", "--extractor", "pypdfium2", "--pdfs", folder)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: no PDF files"))

    def assert_no_detail(self, code: int, out: str, err: str, *kinds: str) -> None:
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(any(f"the run stopped with {kind};" in err for kind in kinds), err)
        self.assertEqual(len(err.strip().splitlines()), 1)  # one line, never a traceback
        for leaked in (PRIVATE_TEXT, "SECRET-FOLDER", "Traceback", ".png", ".pdf"):
            self.assertNotIn(leaked, err)

    def test_a_missing_input_folder_does_not_print_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "SECRET-FOLDER")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                ocr = run("ocr", "--engine", "rapidocr", "--images", missing)
            pdf = run("pdf", "--extractor", "pypdfium2", "--pdfs", missing)
        self.assert_no_detail(*ocr, "FileNotFoundError")
        self.assert_no_detail(*pdf, "FileNotFoundError")

    def test_an_unreadable_output_folder_does_not_print_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "SECRET-FOLDER"
            blocker.write_text("a file where the report folder should be", encoding="utf-8")
            code, out, err = run("vec", "--out", str(blocker / "inside"), "--sizes", "10")
        # The exact error depends on the system: a file in the way is a "not a directory" error on
        # Linux and may be another OSError on Windows.
        self.assert_no_detail(
            code, out, err, "NotADirectoryError", "FileNotFoundError", "FileExistsError"
        )

    def test_a_librarys_error_message_is_never_printed(self) -> None:
        class Leaky(FakeRapid):
            def load(self) -> None:
                raise RuntimeError(f"cannot read {PRIVATE_TEXT}.png in SECRET-FOLDER")

        with tempfile.TemporaryDirectory() as folder:
            images = Path(folder) / "images"
            images.mkdir()
            (images / "a.png").write_text("x", encoding="utf-8")
            (images / "a.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", Leaky):
                result = run("ocr", "--engine", "rapidocr", "--images", str(images))
        self.assert_no_detail(*result, "RuntimeError")

    def test_a_plain_value_error_from_a_library_is_not_printed_either(self) -> None:
        # int("...") and json.loads quote their input, so a ValueError is not one of ours by type.
        class Leaky(FakeRapid):
            def load(self) -> None:
                int(f"{PRIVATE_TEXT} SECRET-FOLDER")

        with tempfile.TemporaryDirectory() as folder:
            images = Path(folder)
            (images / "a.png").write_text("x", encoding="utf-8")
            (images / "a.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", Leaky):
                result = run("ocr", "--engine", "rapidocr", "--images", str(images))
        self.assert_no_detail(*result, "ValueError")

    def test_this_packages_own_errors_are_still_explained(self) -> None:
        code, _, err = run("embedding", "--candidate", "qwen3-embedding-0.6b", "--questions", "no")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))

    def test_missing_inputs_are_named_before_any_model_is_touched(self) -> None:
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(adapters, "SentenceTransformerEmbedder") as embedder,
        ):
            code, out, err = run(
                "embedding",
                "--candidate",
                "qwen3-embedding-0.6b",
                "--corpus",
                folder,
                "--questions",
                str(Path(folder) / "nope.yaml"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot find the corpus manifest", err)
        self.assertIn("and the eval questions", err)
        self.assertIn("demo corpus change", err)
        embedder.assert_not_called()

    def test_only_the_missing_input_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "manifest.json").write_text("{}", encoding="utf-8")
            code, _, err = run(
                "embedding",
                "--candidate",
                "qwen3-embedding-0.6b",
                "--corpus",
                folder,
                "--questions",
                str(Path(folder) / "nope.yaml"),
            )
        self.assertEqual(code, 2)
        self.assertNotIn("manifest", err.split(". The public")[0])
        self.assertIn("the eval questions", err)

    def test_unknown_candidates_are_refused_by_the_parser(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["embedding", "--candidate", "nonsense"])


if __name__ == "__main__":
    unittest.main()

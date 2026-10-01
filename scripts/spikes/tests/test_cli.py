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

PRIVATE_TEXT = "PATIENT-DIAGNOSIS-CONFIDENTIAL"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class FakeRapid:
    name = "fake-rapid"

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
            written = (root / "out" / "ocr-fake-rapid.json").read_text(encoding="utf-8")
            markdown = (root / "out" / "ocr-fake-rapid.md").read_text(encoding="utf-8")
        for text in (written, markdown, out, err):
            self.assertNotIn(PRIVATE_TEXT, text)
        self.assertEqual(json.loads(written)["rows"][0]["metrics"]["cer_mean"], 0.0)

    def test_merge_combines_candidate_reports(self) -> None:
        env = Environment("w", "3.12", "c", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for name in ("a", "b"):
                report = Report("embedding", "T", "Q?", "How.", env)
                report.add(f"model-{name}", chunks=1)
                paths.append(str(report.write(root, stem=f"embedding-{name}")[0]))
            code, _, err = run("merge", *paths, "--out", str(root / "merged"))
            self.assertEqual(code, 0, err)
            merged = json.loads((root / "merged" / "embedding.json").read_text(encoding="utf-8"))
        self.assertEqual([r["candidate"] for r in merged["rows"]], ["model-a", "model-b"])

    def test_errors_are_one_line_on_stderr_with_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, out, err = run("pdf", "--extractor", "pypdfium2", "--pdfs", folder)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: no PDF files"))

    def test_a_missing_corpus_is_an_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, _, err = run(
                "embedding", "--candidate", "qwen3-embedding-0.6b", "--corpus", folder
            )
        self.assertEqual(code, 2)
        self.assertIn("cannot read the manifest", err)

    def test_unknown_candidates_are_refused_by_the_parser(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["embedding", "--candidate", "nonsense"])


if __name__ == "__main__":
    unittest.main()

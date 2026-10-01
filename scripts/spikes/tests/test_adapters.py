"""The parts of the real adapters that can be tested without the libraries or the programs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from vault_spikes import adapters
from vault_spikes.spike_pdf import HEADING_MAX_WORDS, ExtractionError, Outcome


def word(top: float, size: float) -> dict[str, float | str]:
    return {"text": "w", "top": top, "size": size}


class HeadingTest(unittest.TestCase):
    def test_large_short_lines_are_candidates(self) -> None:
        words = [word(10, 20), word(10, 20)]  # a title line
        words += [word(40 + 12 * i, 10) for i in range(30)]  # body lines
        self.assertEqual(adapters.heading_candidates(words), 1)

    def test_a_page_of_one_size_has_none(self) -> None:
        self.assertEqual(adapters.heading_candidates([word(10 + 12 * i, 10) for i in range(20)]), 0)
        self.assertEqual(adapters.heading_candidates([]), 0)

    def test_a_long_large_line_is_not_a_heading(self) -> None:
        words = [word(10, 20) for _ in range(HEADING_MAX_WORDS + 1)]
        words += [word(40 + 12 * i, 10) for i in range(40)]
        self.assertEqual(adapters.heading_candidates(words), 0)

    def test_words_on_one_line_may_differ_a_little_in_height(self) -> None:
        words = [word(10, 20), word(11.5, 20)]
        words += [word(40 + 12 * i, 10) for i in range(30)]
        self.assertEqual(adapters.heading_candidates(words), 1)

    def test_a_mixed_size_line_is_not_a_heading(self) -> None:
        words = [word(10, 20), word(10, 10)]
        words += [word(40 + 12 * i, 10) for i in range(30)]
        self.assertEqual(adapters.heading_candidates(words), 0)


class ClassifyTest(unittest.TestCase):
    def test_library_errors_become_outcomes(self) -> None:
        cases = {
            "Failed to load document (PDFium: Incorrect password error).": Outcome.ENCRYPTED,
            "file is encrypted": Outcome.ENCRYPTED,
            "Data format error": Outcome.CORRUPT,
            "xref table is broken": Outcome.CORRUPT,
            "something else": Outcome.ERROR,
        }
        for message, outcome in cases.items():
            with self.subTest(message):
                self.assertIs(adapters._classify(RuntimeError(message)), outcome)


class MissingLibraryTest(unittest.TestCase):
    def test_each_adapter_says_which_extra_to_install(self) -> None:
        blocked = {
            "sentence_transformers": None,
            "rapidocr": None,
            "pypdfium2": None,
            "pdfplumber": None,
        }
        with mock.patch.dict(sys.modules, blocked):
            for call, extra in (
                (
                    adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu").load,
                    "embedding",
                ),
                (adapters.RapidOcrEngine().load, "ocr"),
                (lambda: adapters.Pypdfium2Extractor().extract(Path("x.pdf")), "pdf"),
                (lambda: adapters.PypdfiumPlumberExtractor().extract(Path("x.pdf")), "pdf"),
            ):
                with (
                    self.subTest(extra=extra),
                    self.assertRaises(adapters.MissingDependencyError) as ctx,
                ):
                    call()
                self.assertIn(f"--extra {extra}", str(ctx.exception))

    def test_a_missing_gpu_library_means_no_gpu(self) -> None:
        with mock.patch.dict(sys.modules, {"torch": None}):
            self.assertIsNone(adapters.gpu_name())


class ValidationTest(unittest.TestCase):
    def test_unknown_candidates_and_devices_are_errors(self) -> None:
        with self.assertRaises(ValueError):
            adapters.SentenceTransformerEmbedder("some-other-model", "cpu")
        with self.assertRaises(ValueError):
            adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "tpu")

    def test_the_candidate_label_includes_the_device(self) -> None:
        embedder = adapters.SentenceTransformerEmbedder("embeddinggemma-300m", "cuda")
        self.assertEqual(embedder.name, "embeddinggemma-300m-cuda")

    def test_encoding_before_loading_is_an_error(self) -> None:
        embedder = adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu")
        with self.assertRaises(RuntimeError):
            embedder.embed_documents(["x"])
        with self.assertRaises(RuntimeError):
            adapters.RapidOcrEngine().recognise(Path("x.png"))
        with self.assertRaises(RuntimeError):
            adapters.TesseractEngine().recognise(Path("x.png"))


class TesseractTest(unittest.TestCase):
    def test_a_missing_program_is_reported_with_a_hint(self) -> None:
        with (
            mock.patch("shutil.which", return_value=None),
            self.assertRaises(adapters.MissingDependencyError) as ctx,
        ):
            adapters.TesseractEngine().load()
        self.assertIn("--tesseract-path", str(ctx.exception))

    def test_the_command_line_is_built_without_a_shell(self) -> None:
        engine = adapters.TesseractEngine("C:/tools/tesseract.exe", "eng")
        engine.load()
        done = mock.Mock(stdout="héllo\n".encode())
        with mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(engine.recognise(Path("scan one.png")), "héllo\n")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["C:/tools/tesseract.exe", "scan one.png", "stdout", "-l", "eng"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["check"])
        self.assertIn("timeout", kwargs)

    def test_the_version_is_the_first_line(self) -> None:
        engine = adapters.TesseractEngine("tess")
        done = mock.Mock(stdout="tesseract 5.5.0\n leptonica-1.84\n", stderr="")
        with mock.patch("subprocess.run", return_value=done):
            self.assertEqual(engine.version(), "tesseract 5.5.0")


class ExtractionErrorTest(unittest.TestCase):
    def test_carries_only_the_outcome(self) -> None:
        error = ExtractionError(Outcome.CORRUPT)
        self.assertEqual(str(error), "corrupt")
        self.assertIs(error.outcome, Outcome.CORRUPT)


if __name__ == "__main__":
    unittest.main()

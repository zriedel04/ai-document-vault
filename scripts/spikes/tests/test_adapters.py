"""The parts of the real adapters that can be tested without the libraries or the programs."""

from __future__ import annotations

import enum
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from vault_spikes import adapters, spike_embedding, spike_ocr, spike_pdf
from vault_spikes.report import PACKAGE_VERSION
from vault_spikes.spike_pdf import HEADING_MAX_WORDS, ExtractionError, Outcome


def word(top: float, size: float, text: str = "w", x0: float = 0.0) -> dict[str, float | str]:
    return {"text": text, "top": top, "size": size, "x0": x0}


class HeadingTest(unittest.TestCase):
    def test_large_short_lines_are_candidates(self) -> None:
        words = [word(10, 20), word(10, 20)]  # a title line
        words += [word(40 + 12 * i, 10) for i in range(30)]  # body lines
        self.assertEqual(len(adapters.heading_candidates(words)), 1)

    def test_the_heading_text_is_returned_left_to_right(self) -> None:
        words = [word(10, 20, "World", 80), word(10, 20, "Hello", 10)]
        words += [word(40 + 12 * i, 10) for i in range(30)]
        self.assertEqual(adapters.heading_candidates(words), ["Hello World"])

    def test_a_page_of_one_size_has_none(self) -> None:
        self.assertEqual(
            len(adapters.heading_candidates([word(10 + 12 * i, 10) for i in range(20)])), 0
        )
        self.assertEqual(len(adapters.heading_candidates([])), 0)

    def test_a_long_large_line_is_not_a_heading(self) -> None:
        words = [word(10, 20) for _ in range(HEADING_MAX_WORDS + 1)]
        words += [word(40 + 12 * i, 10) for i in range(40)]
        self.assertEqual(len(adapters.heading_candidates(words)), 0)

    def test_words_on_one_line_may_differ_a_little_in_height(self) -> None:
        words = [word(10, 20), word(11.5, 20)]
        words += [word(40 + 12 * i, 10) for i in range(30)]
        self.assertEqual(len(adapters.heading_candidates(words)), 1)

    def test_a_mixed_size_line_is_not_a_heading(self) -> None:
        words = [word(10, 20), word(10, 10)]
        words += [word(40 + 12 * i, 10) for i in range(30)]
        self.assertEqual(len(adapters.heading_candidates(words)), 0)


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


class RapidOcrModelTest(unittest.TestCase):
    """The engine is reported as PP-OCRv5, and RapidOCR's own default is another model."""

    @staticmethod
    def fake_rapidocr() -> tuple[types.ModuleType, mock.MagicMock]:
        class OCRVersion(enum.Enum):
            PPOCRV4 = "PP-OCRv4"
            PPOCRV5 = "PP-OCRv5"
            PPOCRV6 = "PP-OCRv6"

        class ModelType(enum.Enum):
            SMALL = "small"
            MOBILE = "mobile"
            SERVER = "server"

        module = types.ModuleType("rapidocr")
        engine = mock.MagicMock()
        module.RapidOCR = engine  # type: ignore[attr-defined]
        module.OCRVersion = OCRVersion  # type: ignore[attr-defined]
        module.ModelType = ModelType  # type: ignore[attr-defined]
        return module, engine

    def test_detection_and_recognition_are_configured_as_pp_ocr_v5(self) -> None:
        module, engine = self.fake_rapidocr()
        with mock.patch.dict(sys.modules, {"rapidocr": module}):
            adapters.RapidOcrEngine().load()
        engine.assert_called_once()
        params = engine.call_args.kwargs["params"]
        for task in ("Det", "Rec"):
            self.assertEqual(params[f"{task}.ocr_version"].value, "PP-OCRv5", task)
            self.assertEqual(params[f"{task}.model_type"].value, "mobile", task)
        self.assertEqual(adapters.RapidOcrEngine.name, "rapidocr-ppocrv5")

    def test_the_declared_floor_is_a_release_that_takes_these_parameters(self) -> None:
        # 3.4.0 is the oldest release checked with these keys (3.0.0 has no such constructor).
        text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"rapidocr>=3.4"', text)


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

    def version_of(self, stdout: str = "", stderr: str = "") -> str:
        engine = adapters.TesseractEngine("tess")
        done = mock.Mock(stdout=stdout, stderr=stderr)
        with mock.patch("subprocess.run", return_value=done):
            return engine.version()

    def test_the_version_is_the_number_in_the_first_line(self) -> None:
        self.assertEqual(self.version_of("tesseract 5.5.0\n leptonica-1.84\n"), "5.5.0")
        self.assertEqual(self.version_of(stderr="tesseract v5.3.3.20231005\n"), "5.3.3.20231005")
        self.assertEqual(self.version_of("tesseract 4.1\n"), "4.1")

    def test_a_first_line_without_a_version_is_unknown_not_text(self) -> None:
        for stdout in ("", "\n", "tesseract\n", "leptonica-1 something\n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(self.version_of(stdout), "unknown")

    def test_what_it_returns_is_always_a_valid_package_version(self) -> None:
        # The last two are numbers too long for a version, which become unknown.
        for stdout in (
            "tesseract 5.5.0\n",
            "tesseract v5.3.3.20231005\n",
            "x\n",
            "",
            "tesseract 1.2.3.4.5.6.7\n",
            "tesseract 12345678901234.1\n",
        ):
            self.assertTrue(PACKAGE_VERSION.fullmatch(self.version_of(stdout)), stdout)


class CorpusPdfReaderTest(unittest.TestCase):
    def test_an_unreadable_pdf_is_none_so_the_corpus_skips_it(self) -> None:
        for message in ("incorrect password", "Data format error", "something else"):
            fake = mock.MagicMock()
            fake.PdfDocument.side_effect = RuntimeError(message)
            with (
                self.subTest(message),
                mock.patch.dict(sys.modules, {"pypdfium2": fake}),
            ):
                self.assertIsNone(adapters.pdf_pages_for_corpus(Path("x.pdf")))

    def test_a_missing_library_still_stops_the_run(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"pypdfium2": None}),
            self.assertRaises(adapters.MissingDependencyError),
        ):
            adapters.pdf_pages_for_corpus(Path("x.pdf"))


class ExtractionErrorTest(unittest.TestCase):
    def test_carries_only_the_outcome(self) -> None:
        error = ExtractionError(Outcome.CORRUPT)
        self.assertEqual(str(error), "corrupt")
        self.assertIs(error.outcome, Outcome.CORRUPT)


class ReportSchemaMatchesTheAdaptersTest(unittest.TestCase):
    """A report may only name candidates the adapters can produce. These tests tie the patterns the
    spikes publish to the names the adapters really use, so adding a model or an engine without
    updating the pattern fails here, not as a merge that refuses a good report."""

    def test_every_embedding_model_on_both_devices_is_a_known_candidate(self) -> None:
        for candidate in adapters.EMBEDDING_MODELS:
            for device in ("cpu", "cuda"):
                name = adapters.SentenceTransformerEmbedder(candidate, device).name
                with self.subTest(name=name):
                    self.assertTrue(spike_embedding.CANDIDATES.fullmatch(name))

    def test_the_embedding_pattern_allows_nothing_else(self) -> None:
        for name in ("qwen3-embedding-0.6b", "embeddinggemma-300m-tpu", "x-cpu", "model-cpu\n"):
            with self.subTest(name=name):
                self.assertIsNone(spike_embedding.CANDIDATES.fullmatch(name))

    def test_the_ocr_engines_are_known_candidates(self) -> None:
        for engine in (adapters.RapidOcrEngine, adapters.TesseractEngine):
            self.assertTrue(spike_ocr.CANDIDATES.fullmatch(engine.name), engine.name)
        self.assertIn("tesseract", spike_ocr.REPORT_PACKAGES)  # added by the command line

    def test_the_pdf_extractors_are_known_candidates(self) -> None:
        for extractor in (adapters.Pypdfium2Extractor, adapters.PypdfiumPlumberExtractor):
            self.assertTrue(spike_pdf.CANDIDATES.fullmatch(extractor.name), extractor.name)


if __name__ == "__main__":
    unittest.main()


class RequireCudaTest(unittest.TestCase):
    def fake_torch(self, available: bool) -> mock.MagicMock:
        torch = mock.MagicMock()
        torch.cuda.is_available.return_value = available
        return torch

    def test_a_cpu_only_torch_is_explained(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"torch": self.fake_torch(False)}),
            self.assertRaises(adapters.MissingDependencyError) as caught,
        ):
            adapters.require_cuda()
        self.assertIn("pytorch.org", str(caught.exception))
        self.assertIn("--no-sync", str(caught.exception))

    def test_a_cuda_torch_passes(self) -> None:
        with mock.patch.dict(sys.modules, {"torch": self.fake_torch(True)}):
            adapters.require_cuda()

    def test_a_missing_torch_names_the_extra(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"torch": None}),
            self.assertRaises(adapters.MissingDependencyError) as caught,
        ):
            adapters.require_cuda()
        self.assertIn("--extra embedding", str(caught.exception))

    def test_the_cpu_embedder_does_not_ask_for_cuda(self) -> None:
        # load() on the CPU path must not call require_cuda (it would fail on a CPU-only torch).
        embedder = adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu")
        fake_st = mock.MagicMock()
        with (
            mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}),
            mock.patch.object(adapters, "require_cuda") as require,
        ):
            embedder.load()
        require.assert_not_called()

    def test_the_model_is_loaded_from_the_local_cache_only(self) -> None:
        embedder = adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu")
        fake_st = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            embedder.load()
        fake_st.SentenceTransformer.assert_called_once_with(
            "Qwen/Qwen3-Embedding-0.6B", device="cpu", local_files_only=True
        )

    def test_the_reported_batch_size_is_the_one_the_encoder_uses(self) -> None:
        # sentence-transformers batches by 32 unless told otherwise, so a report that says 64
        # would describe a run that was really done in batches of 32.
        for query in (False, True):
            embedder = adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu", 64)
            model = mock.MagicMock()
            model.encode_document.return_value.tolist.return_value = [[1.0]]
            model.encode_query.return_value.tolist.return_value = [[1.0]]
            embedder.model = model
            with self.subTest(query=query):
                (embedder.embed_queries if query else embedder.embed_documents)(["a"])
                called = model.encode_query if query else model.encode_document
                self.assertEqual(called.call_args.kwargs["batch_size"], 64)
        # Older versions without encode_query / encode_document use plain encode.
        old = mock.MagicMock(spec=["encode"])
        old.encode.return_value.tolist.return_value = [[1.0]]
        embedder.model = old
        embedder.embed_documents(["a"])
        self.assertEqual(old.encode.call_args.kwargs["batch_size"], 64)
        self.assertEqual(
            adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cpu").batch_size,
            adapters.DEFAULT_BATCH_SIZE,
        )

    def test_a_batch_size_that_cannot_work_is_refused(self) -> None:
        for bad in (0, -1, True, 1.5, "16"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                adapters.SentenceTransformerEmbedder(
                    "qwen3-embedding-0.6b",
                    "cpu",
                    bad,  # type: ignore[arg-type]
                )

    def test_a_model_that_is_not_downloaded_says_how_to_fetch_it(self) -> None:
        embedder = adapters.SentenceTransformerEmbedder("embeddinggemma-300m", "cpu")
        fake_st = mock.MagicMock()
        fake_st.SentenceTransformer.side_effect = OSError("not in cache")
        with (
            mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}),
            self.assertRaises(adapters.MissingDependencyError) as caught,
        ):
            embedder.load()
        self.assertIn("fetch-model --candidate embeddinggemma-300m", str(caught.exception))

    def test_fetching_downloads_without_the_local_only_restriction(self) -> None:
        fake_st = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            adapters.fetch_model("embeddinggemma-300m")
        fake_st.SentenceTransformer.assert_called_once_with(
            "google/embeddinggemma-300m", device="cpu"
        )

    def test_a_failed_download_is_explained(self) -> None:
        fake_st = mock.MagicMock()
        fake_st.SentenceTransformer.side_effect = OSError("401")
        with (
            mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}),
            self.assertRaises(adapters.MissingDependencyError) as caught,
        ):
            adapters.fetch_model("embeddinggemma-300m")
        self.assertIn("hf auth login", str(caught.exception))
        self.assertNotIn("401", str(caught.exception))

    def test_fetching_an_unknown_model_or_without_the_library_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            adapters.fetch_model("some-other-model")
        with (
            mock.patch.dict(sys.modules, {"sentence_transformers": None}),
            self.assertRaises(adapters.MissingDependencyError),
        ):
            adapters.fetch_model("qwen3-embedding-0.6b")

    def test_the_cuda_embedder_checks_the_gpu_before_loading_the_model(self) -> None:
        embedder = adapters.SentenceTransformerEmbedder("qwen3-embedding-0.6b", "cuda")
        fake_st = mock.MagicMock()
        with (
            mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}),
            mock.patch.object(
                adapters, "require_cuda", side_effect=adapters.MissingDependencyError
            ),
            self.assertRaises(adapters.MissingDependencyError),
        ):
            embedder.load()
        fake_st.SentenceTransformer.assert_not_called()


class ModelRevisionLookupTest(unittest.TestCase):
    REVISION = "0123456789abcdef0123456789abcdef01234567"

    def hub(self, answer: object) -> dict[str, object]:
        module = types.ModuleType("huggingface_hub")
        module.snapshot_download = mock.Mock(  # type: ignore[attr-defined]
            side_effect=answer if isinstance(answer, Exception) else None,
            return_value=answer,
        )
        return {"huggingface_hub": module}

    def test_the_revision_is_the_name_of_the_cached_snapshot_folder(self) -> None:
        path = f"/cache/models--Qwen--Qwen3-Embedding-0.6B/snapshots/{self.REVISION}"
        modules = self.hub(path)
        with mock.patch.dict(sys.modules, modules):
            self.assertEqual(adapters.model_revision("qwen3-embedding-0.6b"), self.REVISION)
        # Looked up in the cache only, for the model the candidate names, never downloaded.
        modules["huggingface_hub"].snapshot_download.assert_called_once_with(  # type: ignore[attr-defined]
            "Qwen/Qwen3-Embedding-0.6B", local_files_only=True
        )

    def test_anything_else_is_unknown(self) -> None:
        cases = (
            self.hub("/cache/snapshots/main"),
            self.hub(f"/cache/snapshots/{self.REVISION}\n"),
            self.hub(OSError("/private/folder is not readable")),
            {"huggingface_hub": None},  # the library is missing
        )
        for modules in cases:
            with mock.patch.dict(sys.modules, modules):
                self.assertIsNone(adapters.model_revision("embeddinggemma-300m"))
        self.assertIsNone(adapters.model_revision("not-a-candidate"))

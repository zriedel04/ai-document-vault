"""The four spikes, run end to end against fakes. Nothing here loads a model or a library."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Iterable, Sequence
from pathlib import Path

from vault_spikes import corpus as c
from vault_spikes import spike_embedding as emb
from vault_spikes import spike_ocr as ocr
from vault_spikes import spike_pdf as pdf
from vault_spikes import spike_vec as vec
from vault_spikes.report import Environment, Report

ENV = Environment("Windows-11", "3.12.1", "cpu", 8, 16.0, None, {})
PRIVATE_TEXT = "PATIENT-DIAGNOSIS-CONFIDENTIAL"


class Clock:
    """A fake clock that advances by a fixed step each time it is read."""

    def __init__(self, step: float = 0.5) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def everything(report: Report) -> str:
    return json.dumps(report.to_json()) + report.to_markdown()


# --- vec ------------------------------------------------------------------------------------


class VecTest(unittest.TestCase):
    def test_the_spike_reports_each_size_and_a_correct_store(self) -> None:
        report = vec.run_vec_spike(
            lambda _dim: vec.PythonStore(),
            sizes=(50, 120),
            dim=8,
            queries=4,
            k=3,
            check_size=40,
            environment=ENV,
            clock=Clock(),
        )
        rows = {row.candidate: row.metrics for row in report.rows}
        self.assertEqual(set(rows), {"exact-search-check", "vectors-50", "vectors-120"})
        self.assertEqual(rows["exact-search-check"]["same_answer_as_python"], 1.0)
        for name in ("vectors-50", "vectors-120"):
            self.assertEqual(rows[name]["dim"], 8)
            self.assertGreater(rows[name]["insert_vectors_per_second"], 0)
            self.assertIn("query_ms_p95", rows[name])

    def test_only_the_inserts_are_timed(self) -> None:
        # The clock moves only inside ``insert`` (2 s per batch of 1000), so the rate is the
        # inserted count over the time spent inserting, whatever else the spike does.
        clock = Clock(step=0.0)

        class Slow(vec.PythonStore):
            def insert(self, rows: Iterable[tuple[int, bytes]]) -> None:
                super().insert(rows)
                clock.now += 2.0

        report = vec.run_vec_spike(
            lambda _dim: Slow(),
            sizes=(2500,),
            dim=4,
            queries=1,
            k=3,
            check_size=10,
            environment=ENV,
            clock=clock,
        )
        row = next(r for r in report.rows if r.candidate == "vectors-2500")
        # 2500 vectors in three batches: 3 x 2 s of insert time. The check-size insert is not
        # part of the measured size.
        self.assertEqual(row.metrics["insert_vectors_per_second"], round(2500 / 6.0, 4))

    def test_a_wrong_store_is_noticed(self) -> None:
        class Wrong(vec.PythonStore):
            def nearest(self, query: bytes, k: int) -> list[int]:
                return list(range(1000, 1000 + k))

        report = vec.run_vec_spike(
            lambda _dim: Wrong(), sizes=(), dim=4, check_size=20, environment=ENV, clock=Clock()
        )
        self.assertEqual(report.rows[0].metrics["same_answer_as_python"], 0.0)

    def test_a_store_that_returns_too_few_neighbours_is_an_error(self) -> None:
        class Short(vec.PythonStore):
            def nearest(self, query: bytes, k: int) -> list[int]:
                return super().nearest(query, k)[:1]

        with self.assertRaises(RuntimeError):
            vec.run_vec_spike(
                lambda _dim: Short(),
                sizes=(30,),
                dim=4,
                queries=1,
                k=3,
                check_size=10,
                environment=ENV,
                clock=Clock(),
            )

    def test_an_extension_that_does_not_load_is_the_whole_result(self) -> None:
        def explode(_dim: int) -> vec.VectorStore:
            raise AssertionError("no store may be built")

        report = vec.run_vec_spike(
            explode, loading={"can_load_extensions": False, "vec_loads": False}, environment=ENV
        )
        self.assertEqual([r.candidate for r in report.rows], ["extension-loading"])
        self.assertIs(report.rows[0].metrics["vec_loads"], False)

    def test_random_vectors_are_repeatable(self) -> None:
        self.assertEqual(list(vec.random_vectors(3, 4, 1)), list(vec.random_vectors(3, 4, 1)))
        self.assertNotEqual(list(vec.random_vectors(3, 4, 1)), list(vec.random_vectors(3, 4, 2)))
        self.assertEqual({len(v) for v in vec.random_vectors(3, 4, 1)}, {16})

    def test_probe_reports_facts_without_failing(self) -> None:
        facts = vec.probe_extension_loading()
        self.assertEqual(set(facts), {"can_load_extensions", "vec_loads"})


# --- embedding ------------------------------------------------------------------------------


class FakeEmbedder:
    """Embeds text as counts of three words, so retrieval works without a model."""

    name = "fake-model-cpu"

    def __init__(self) -> None:
        self.loaded = False
        self.calls = 0

    def load(self) -> None:
        self.loaded = True

    def _vector(self, text: str) -> list[float]:
        lower = text.lower()
        return [float(lower.count(w)) + 0.01 for w in ("instructor", "budget", "deadline")]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


def corpus_inputs() -> emb.EmbeddingInputs:
    documents = [
        c.Document("a.txt", "text", None, False, None),
        c.Document("b.txt", "text", None, False, None),
        c.Document("scan.pdf", "pdf_scan", 1, True, None),
    ]
    chunks = [
        c.Chunk("a.txt", 1, "The instructor is Dr. Example. Ask the instructor."),
        c.Chunk("b.txt", 1, f"The budget total is 10. The budget is final. {PRIVATE_TEXT}"),
    ]
    questions = [
        c.Question("q1", "Who is the instructor?", "lookup", True, ("a.txt",), ()),
        c.Question("q2", "What is the budget?", "lookup", True, ("b.txt",), ()),
        c.Question("q3", "What does the scan say?", "ocr", True, ("scan.pdf",), ()),
        c.Question("q4", "Unanswerable?", "not_in_vault", False, (), ()),
    ]
    corpus = c.Corpus(chunks, frozenset({"a.txt", "b.txt"}), skipped=1)
    return emb.EmbeddingInputs(corpus, documents, questions)


class EmbeddingTest(unittest.TestCase):
    def test_one_candidate_is_measured_and_scored(self) -> None:
        embedder = FakeEmbedder()
        report = emb.run_embedding_spike(
            embedder,
            corpus_inputs(),
            batch_size=1,
            vault_chunks=7200,
            environment=ENV,
            clock=Clock(step=1.0),
        )
        self.assertTrue(embedder.loaded)
        metrics = report.rows[0].metrics
        self.assertEqual(report.rows[0].candidate, "fake-model-cpu")
        self.assertEqual(metrics["recall_at_10"], 1.0)
        self.assertEqual(metrics["hit_at_1"], 1.0)
        self.assertEqual(metrics["questions_scored"], 2)
        self.assertEqual(metrics["questions_left_out"], 1)  # the scan question
        self.assertEqual(metrics["chunks"], 2)
        self.assertEqual(metrics["dimensions"], 3)
        self.assertEqual(metrics["load_seconds"], 1.0)
        # 2 chunks in 1 second of fake time: 2 per second, so 7200 chunks take one hour.
        self.assertEqual(metrics["chunks_per_second"], 2.0)
        self.assertEqual(metrics["hours_for_vault_of_chunks"], 1.0)

    def test_a_warm_up_call_is_made_before_timing(self) -> None:
        embedder = FakeEmbedder()
        emb.run_embedding_spike(
            embedder, corpus_inputs(), batch_size=1, environment=ENV, clock=Clock()
        )
        self.assertEqual(embedder.calls, 3)  # one warm-up and two timed batches

    def test_document_text_never_reaches_the_report(self) -> None:
        report = emb.run_embedding_spike(
            FakeEmbedder(), corpus_inputs(), environment=ENV, clock=Clock()
        )
        self.assertNotIn(PRIVATE_TEXT, everything(report))

    def test_an_empty_corpus_is_an_error(self) -> None:
        inputs = emb.EmbeddingInputs(c.Corpus([], frozenset(), 0), [], [])
        with self.assertRaises(ValueError):
            emb.run_embedding_spike(FakeEmbedder(), inputs, environment=ENV)

    def test_a_model_that_drops_vectors_is_an_error(self) -> None:
        class Dropping(FakeEmbedder):
            def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
                return super().embed_documents(texts)[:-1]

        with self.assertRaises(RuntimeError):
            emb.run_embedding_spike(
                Dropping(), corpus_inputs(), batch_size=2, environment=ENV, clock=Clock()
            )

    def test_without_scoreable_questions_only_speed_is_reported(self) -> None:
        inputs = corpus_inputs()
        inputs = emb.EmbeddingInputs(inputs.corpus, inputs.documents, inputs.questions[2:])
        report = emb.run_embedding_spike(FakeEmbedder(), inputs, environment=ENV, clock=Clock())
        metrics = report.rows[0].metrics
        self.assertNotIn("recall_at_10", metrics)
        self.assertEqual(metrics["questions_scored"], 0)


# --- ocr ------------------------------------------------------------------------------------


class FakeOcr:
    name = "fake-ocr"

    def __init__(self, texts: dict[str, str], broken: frozenset[str] = frozenset()) -> None:
        self.texts = texts
        self.broken = broken
        self.loaded = False

    def load(self) -> None:
        self.loaded = True

    def recognise(self, image: Path) -> str:
        if image.name in self.broken:
            raise RuntimeError(PRIVATE_TEXT)  # a message that must never be reported
        return self.texts[image.name]


class OcrTest(unittest.TestCase):
    def samples(self) -> list[ocr.LabelledImage]:
        return [
            ocr.LabelledImage(Path("a.png"), "hello world"),
            ocr.LabelledImage(Path("b.png"), "total 100"),
            ocr.LabelledImage(Path("c.png"), "receipt"),
        ]

    def test_accuracy_and_speed_are_reported(self) -> None:
        engine = FakeOcr({"a.png": "hello world", "b.png": "total 108", "c.png": ""})
        report = ocr.run_ocr_spike(engine, self.samples(), environment=ENV, clock=Clock(step=1.0))
        metrics = report.rows[0].metrics
        self.assertTrue(engine.loaded)
        self.assertEqual(metrics["images"], 3)
        self.assertEqual(metrics["failures"], 0)
        self.assertEqual(metrics["empty_results"], 1)
        self.assertAlmostEqual(metrics["cer_mean"], (0 + 1 / 9 + 1) / 3, places=3)
        self.assertEqual(metrics["seconds_per_image_p50"], 1.0)
        self.assertEqual(metrics["images_per_minute"], 60.0)

    def test_failures_are_counted_and_their_messages_never_reported(self) -> None:
        engine = FakeOcr({"a.png": "hello world", "c.png": "receipt"}, broken=frozenset({"b.png"}))
        report = ocr.run_ocr_spike(engine, self.samples(), environment=ENV, clock=Clock())
        self.assertEqual(report.rows[0].metrics["failures"], 1)
        self.assertNotIn(PRIVATE_TEXT, everything(report))

    def test_recognised_and_reference_text_never_reach_the_report(self) -> None:
        samples = [ocr.LabelledImage(Path("a.png"), f"{PRIVATE_TEXT} alpha")]
        engine = FakeOcr({"a.png": f"{PRIVATE_TEXT} alpha"})
        report = ocr.run_ocr_spike(engine, samples, environment=ENV, clock=Clock())
        self.assertNotIn(PRIVATE_TEXT, everything(report))
        self.assertEqual(report.rows[0].metrics["cer_mean"], 0.0)

    def test_an_empty_reference_is_set_aside_not_scored(self) -> None:
        samples = [ocr.LabelledImage(Path("a.png"), "  "), ocr.LabelledImage(Path("b.png"), "x")]
        report = ocr.run_ocr_spike(
            FakeOcr({"a.png": "x", "b.png": "x"}), samples, environment=ENV, clock=Clock()
        )
        self.assertEqual(report.rows[0].metrics["references_unusable"], 1)
        self.assertEqual(report.rows[0].metrics["cer_mean"], 0.0)

    def test_an_engine_that_fails_everywhere_still_reports(self) -> None:
        engine = FakeOcr({}, broken=frozenset({"a.png", "b.png", "c.png"}))
        report = ocr.run_ocr_spike(engine, self.samples(), environment=ENV, clock=Clock())
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["failures"], 3)
        self.assertNotIn("cer_mean", metrics)

    def test_no_samples_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            ocr.run_ocr_spike(FakeOcr({}), [], environment=ENV)

    def test_images_are_paired_with_their_reference_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("a.png", "b.JPG", "c.png", "notes.pdf"):
                (root / name).write_bytes(b"x")
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            (root / "b.txt").write_text("beta", encoding="utf-8")
            (root / "notes.txt").write_text("not an image", encoding="utf-8")
            found = ocr.find_labelled_images(root)
            self.assertEqual(
                [(s.image.name, s.reference) for s in found],
                [("a.png", "alpha"), ("b.JPG", "beta")],
            )
            references = root / "refs"
            references.mkdir()
            (references / "c.txt").write_text("gamma", encoding="utf-8")
            apart = ocr.find_labelled_images(root, references)
            self.assertEqual([s.image.name for s in apart], ["c.png"])


# --- pdf ------------------------------------------------------------------------------------


class FakePdf:
    name = "fake-pdf"

    def __init__(self, results: dict[str, pdf.PdfText | pdf.Outcome | Exception]) -> None:
        self.results = results

    def extract(self, path: Path) -> pdf.PdfText:
        result = self.results[path.name]
        if isinstance(result, pdf.Outcome):
            raise pdf.ExtractionError(result)
        if isinstance(result, Exception):
            raise result
        return result


class PdfTest(unittest.TestCase):
    def test_outcomes_pages_headings_and_speed(self) -> None:
        extractor = FakePdf(
            {
                "ok.pdf": pdf.PdfText(["page one", "", "page three"], 4),
                "locked.pdf": pdf.Outcome.ENCRYPTED,
                "broken.pdf": pdf.Outcome.CORRUPT,
                "weird.pdf": RuntimeError(PRIVATE_TEXT),
            }
        )
        pdfs = [Path(n) for n in ("ok.pdf", "locked.pdf", "broken.pdf", "weird.pdf")]
        report = pdf.run_pdf_spike(extractor, pdfs, environment=ENV, clock=Clock(step=1.0))
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["files"], 4)
        self.assertEqual(metrics["files_ok"], 1)
        self.assertEqual(metrics["files_encrypted"], 1)
        self.assertEqual(metrics["files_corrupt"], 1)
        self.assertEqual(metrics["files_other_error"], 1)
        self.assertEqual(metrics["pages"], 3)
        self.assertEqual(metrics["pages_without_text"], 1)
        self.assertEqual(metrics["heading_candidates"], 4)
        self.assertEqual(metrics["files_with_heading_candidates"], 1)
        self.assertEqual(metrics["pages_per_second"], 3 / 4)  # four files, one second each
        self.assertNotIn(PRIVATE_TEXT, everything(report))

    def test_word_error_rate_against_a_reference_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ok.pdf").write_bytes(b"x")
            (root / "ok.txt").write_text("the quick brown fox", encoding="utf-8")
            (root / "empty.pdf").write_bytes(b"x")
            (root / "empty.txt").write_text("  ", encoding="utf-8")
            extractor = FakePdf(
                {
                    "ok.pdf": pdf.PdfText(["the quick brown dog"], 0),
                    "empty.pdf": pdf.PdfText(["text"], 0),
                }
            )
            report = pdf.run_pdf_spike(
                extractor, [root / "ok.pdf", root / "empty.pdf"], environment=ENV, clock=Clock()
            )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["references_used"], 1)
        self.assertEqual(metrics["wer_mean"], 0.25)

    def test_no_pdfs_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            pdf.run_pdf_spike(FakePdf({}), [], environment=ENV)


if __name__ == "__main__":
    unittest.main()

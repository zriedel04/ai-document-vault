"""The four spikes, run end to end against fakes. Nothing here loads a model or a library."""

from __future__ import annotations

import array
import codecs
import json
import tempfile
import unittest
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from unittest import mock

from vault_spikes import adapters
from vault_spikes import corpus as c
from vault_spikes import spike_embedding as emb
from vault_spikes import spike_ocr as ocr
from vault_spikes import spike_pdf as pdf
from vault_spikes import spike_vec as vec
from vault_spikes.__main__ import KNOWN
from vault_spikes.errors import OwnValueError
from vault_spikes.report import Environment, Report, in_domain

ENV = Environment("Windows-11", "3.12.1", "cpu", 8, 16.0, None, {})
PRIVATE_TEXT = "PATIENT-DIAGNOSIS-CONFIDENTIAL"

EMITTED: dict[str, set[str]] = {}
VALUES: list[tuple[str, str, object]] = []
ROW_KEYS: list[tuple[str, str, dict[str, float]]] = []
_real_add = Report.add


def setUpModule() -> None:
    """Record every metric name the spikes emit during these tests, to compare with the schemas."""

    def recording_add(self: Report, candidate: str, **metrics: float) -> None:
        EMITTED.setdefault(self.spike, set()).update(metrics)
        VALUES.extend((self.spike, name, value) for name, value in metrics.items())
        ROW_KEYS.append((self.spike, candidate, dict(metrics)))
        _real_add(self, candidate, **metrics)

    Report.add = recording_add  # type: ignore[method-assign]


def tearDownModule() -> None:
    """Every metric a spike emitted must be in its schema, or a good report could not be merged."""
    Report.add = _real_add  # type: ignore[method-assign]
    schemas = {"embedding": emb.METRICS, "ocr": ocr.METRICS, "pdf": pdf.METRICS, "vec": vec.METRICS}
    for spike, names in EMITTED.items():
        unknown = names - set(schemas[spike])
        assert not unknown, f"{spike} emits metrics its schema does not list: {sorted(unknown)}"
    assert set(EMITTED) == set(schemas), "a spike was not exercised by these tests"
    # Each row a spike wrote must fit the shape its candidate has, or merge would refuse the
    # report the script itself wrote; and no metric of the vocabulary may be one no row can have.
    for spike, candidate, row in ROW_KEYS:
        shape = KNOWN[spike].shape_of(candidate)
        assert shape is not None, f"{spike}: no row shape fits a candidate it emits"
        emitted = frozenset(row)
        assert emitted <= shape.allowed, (
            f"{spike}/{candidate} emits {sorted(emitted - shape.allowed)}"
        )
        assert not shape.broken_groups(emitted), f"{spike}/{candidate} gives half a group"
        assert shape.required <= emitted, (
            f"{spike}/{candidate} lacks {sorted(shape.required - emitted)}"
        )
        assert shape.consistent is None or shape.consistent(row), (
            f"{spike}/{candidate} writes figures that do not fit together: {row}"
        )
        assert shape.candidate_fits is None or shape.candidate_fits(candidate, row), (
            f"{spike}/{candidate} names a figure its row does not have: {row}"
        )
    for spike, schema in KNOWN.items():
        used = {schema.shape_of(candidate) for name, candidate, _ in ROW_KEYS if name == spike}
        assert set(schema.rows) <= used, f"{spike}: a row shape no test exercised"
        allowed: set[str] = set().union(*(shape.allowed for shape in schema.rows))
        assert allowed == set(schema.metrics), f"{spike}: a metric no row shape allows"
    # A value a spike really produces must be one its metric's kind allows, or the report that
    # script wrote could not be merged.
    for spike, name, value in VALUES:
        assert in_domain(schemas[spike][name], value), f"{spike}: {name} is outside its kind"


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


class PlainTable(vec.SqliteVecStore):
    """A real database file without the extension, which is not installed here: the vectors go
    into a plain table, so what is tested is the store's file handling, not sqlite-vec."""

    def _load_extension(self) -> None:
        pass

    def _create_table(self, dim: int) -> None:
        self.db.execute("CREATE TABLE v(rowid INTEGER PRIMARY KEY, embedding BLOB)")

    def nearest(self, query: bytes, k: int) -> list[int]:
        return [int(row[0]) for row in self.db.execute("SELECT rowid FROM v LIMIT ?", (k,))]


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
            self.assertEqual(rows[name]["vectors"], int(name.removeprefix("vectors-")))
            self.assertEqual(rows[name]["dim"], 8)
            self.assertEqual(rows[name]["queries_timed"], 4)  # the p95 rests on this many
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

    def test_several_real_stores_can_share_a_folder_and_clean_up_after_themselves(self) -> None:
        # The run builds a store for the correctness check and one per size, all in one folder.
        # sqlite-vec is not available here, so the extension and the table are replaced with a
        # plain table; what is tested is that each store has a database file of its own.
        with tempfile.TemporaryDirectory() as folder:
            report = vec.run_vec_spike(
                lambda dim: PlainTable(dim, Path(folder)),
                sizes=(30, 40),
                dim=4,
                queries=1,
                k=3,
                check_size=10,
                environment=ENV,
                clock=Clock(),
            )
            self.assertEqual([r.candidate for r in report.rows][-2:], ["vectors-30", "vectors-40"])
            self.assertEqual(list(Path(folder).iterdir()), [])  # every database was removed

    def test_sizes_out_of_order_or_repeated_are_refused_before_a_store_is_made(self) -> None:
        """The peak is the process's high-water mark: a larger size run first would put its peak
        in the row of every smaller one."""

        def refuse(_dim: int) -> vec.PythonStore:
            raise AssertionError("no store should be made")

        for sizes in ((100_000, 10_000), (50, 50), (10, 50, 20)):
            with self.subTest(sizes=sizes), self.assertRaises(OwnValueError):
                vec.run_vec_spike(refuse, sizes=sizes, dim=4, check_size=10, environment=ENV)
        vec.check_sizes(())
        vec.check_sizes((7,))
        vec.check_sizes((10, 20, 30))

    def test_a_size_no_row_name_can_state_is_refused_before_a_store_is_made(self) -> None:
        """A row is named ``vectors-N`` with N from 1 to nine digits; any other size would be
        measured and then refused by ``merge``."""

        def refuse(_dim: int) -> vec.PythonStore:
            raise AssertionError("no store should be made")

        for sizes in ((0,), (-5, 10), (10, vec.LARGEST_SIZE + 1)):
            with self.subTest(sizes=sizes), self.assertRaises(OwnValueError) as caught:
                vec.run_vec_spike(refuse, sizes=sizes, dim=4, check_size=10, environment=ENV)
            self.assertIn("from 1 to 999,999,999", str(caught.exception))
        vec.check_sizes((1, vec.LARGEST_SIZE))
        self.assertIsNotNone(vec.CANDIDATES.fullmatch(f"vectors-{vec.LARGEST_SIZE}"))
        self.assertIsNone(vec.CANDIDATES.fullmatch(f"vectors-{vec.LARGEST_SIZE + 1}"))

    def test_a_real_database_holds_at_least_the_bytes_of_its_vectors(self) -> None:
        """``merge`` refuses a size row whose database is smaller than ``vectors * dim * 4``
        bytes; a database file that stores the vectors is never that small."""

        with tempfile.TemporaryDirectory() as folder:
            report = vec.run_vec_spike(
                lambda dim: PlainTable(dim, Path(folder)),
                sizes=(3000,),
                dim=16,
                queries=1,
                k=3,
                check_size=10,
                environment=ENV,
                clock=Clock(),
            )
        row = report.rows[-1].metrics
        self.assertGreaterEqual(row["database_mib"], 3000 * 16 * vec.FLOAT32_BYTES / 1024**2)

    def test_each_row_peak_is_its_own_when_sizes_ascend(self) -> None:
        peaks = iter([111.0, 222.0])
        with mock.patch.object(vec, "peak_memory_mib", side_effect=lambda: next(peaks)):
            report = vec.run_vec_spike(
                lambda _dim: vec.PythonStore(),
                sizes=(20, 40),
                dim=4,
                queries=1,
                k=3,
                check_size=10,
                environment=ENV,
                clock=Clock(),
            )
        measured = {r.candidate: r.metrics["peak_memory_mib"] for r in report.rows[1:]}
        self.assertEqual(measured, {"vectors-20": 111.0, "vectors-40": 222.0})

    def test_a_wrong_store_is_noticed(self) -> None:
        class Wrong(vec.PythonStore):
            def nearest(self, query: bytes, k: int) -> list[int]:
                return list(range(1000, 1000 + k))

        report = vec.run_vec_spike(
            lambda _dim: Wrong(), sizes=(), dim=4, check_size=20, environment=ENV, clock=Clock()
        )
        self.assertEqual(report.rows[0].metrics["same_answer_as_python"], 0.0)

    def test_the_right_neighbours_in_the_wrong_order_are_not_the_right_answer(self) -> None:
        """A store that returns the exact ten ids from farthest to nearest has a ranking the
        application cannot use, though the sets are equal."""

        class Reversed(vec.PythonStore):
            def nearest(self, query: bytes, k: int) -> list[int]:
                return super().nearest(query, k)[::-1]

        report = vec.run_vec_spike(
            lambda _dim: Reversed(),
            sizes=(),
            dim=4,
            k=5,
            check_size=30,
            environment=ENV,
            clock=Clock(),
        )
        self.assertEqual(report.rows[0].metrics["same_answer_as_python"], 0.0)

    def test_ranking_rules(self) -> None:
        def blob(*values: float) -> bytes:
            return array.array("f", values).tobytes()

        vectors = [blob(0.0), blob(1.0), blob(1.0), blob(5.0)]  # ids 2 and 3 tie
        query = blob(0.0)
        self.assertEqual(vec.l2_nearest(vectors, query, 3), [1, 2, 3])
        self.assertTrue(vec.same_ranking([1, 2, 3], vectors, query, 3))
        self.assertTrue(vec.same_ranking([1, 3, 2], vectors, query, 3), "a tie may swap")
        # The tie at the edge of the ten: either of the equal vectors may be the last one.
        self.assertTrue(vec.same_ranking([1, 3], vectors, query, 2))
        for wrong in ([2, 1, 3], [1, 2, 4], [1, 2], [1, 2, 3, 4], [1, 2, 2], [1, 2, 0], [1, 2, 9]):
            with self.subTest(found=wrong):
                self.assertFalse(vec.same_ranking(wrong, vectors, query, 3))

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

    name = "qwen3-embedding-0.6b-cpu"

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
        c.Question(
            "q1",
            "Who is the instructor?",
            "lookup",
            True,
            (c.Source("a.txt", "Dr. Example"),),
            (),
            find_document=True,
        ),
        c.Question(
            "q2",
            "What is the budget?",
            "lookup",
            True,
            (c.Source("b.txt", "total is 10"),),
            (),
            find_document=True,
        ),
        c.Question(
            "q3", "What does the scan say?", "ocr", True, (c.Source("scan.pdf", "scanned"),), ()
        ),
        c.Question("q4", "Unanswerable?", "not_in_vault", False, (), ()),
    ]
    corpus = c.Corpus(chunks, frozenset({"a.txt", "b.txt"}), skipped=1)
    return emb.EmbeddingInputs(corpus, documents, questions)


class EmbeddingTest(unittest.TestCase):
    def test_the_method_does_not_call_the_questions_public(self) -> None:
        # The same text heads a report on the owner's private questions, where "public" is false.
        # Only the sentence that explains the `public_dataset` metric may use the word.
        _, _, rest = emb.METHOD.partition("`public_dataset` metric")
        sentences = emb.METHOD.replace(rest, "").replace("`public_dataset` metric", "")
        self.assertNotIn("public", sentences.lower())
        self.assertIn("Each answerable evaluation question", emb.METHOD)

    def test_a_gpu_candidate_reports_video_memory_next_to_host_memory(self) -> None:
        embedder = FakeEmbedder()
        embedder.name = "qwen3-embedding-0.6b-cuda"
        report = emb.run_embedding_spike(
            embedder, corpus_inputs(), environment=ENV, gpu_memory=lambda: (1500.5, 2048.0)
        )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["gpu_peak_allocated_mib"], 1500.5)
        self.assertEqual(metrics["gpu_peak_reserved_mib"], 2048.0)
        self.assertIn("peak_memory_mib", metrics)  # host RAM stays, it is a different number

    def test_a_cpu_candidate_or_a_missing_gpu_reports_no_video_memory(self) -> None:
        def unexpected() -> tuple[float, float]:
            raise AssertionError("a CPU run must not ask for video memory")

        cpu = emb.run_embedding_spike(
            FakeEmbedder(), corpus_inputs(), environment=ENV, gpu_memory=unexpected
        )
        self.assertNotIn("gpu_peak_allocated_mib", cpu.rows[0].metrics)
        gpu = FakeEmbedder()
        gpu.name = "qwen3-embedding-0.6b-cuda"
        none = emb.run_embedding_spike(
            gpu, corpus_inputs(), environment=ENV, gpu_memory=lambda: None
        )
        self.assertNotIn("gpu_peak_reserved_mib", none.rows[0].metrics)

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
        self.assertEqual(report.rows[0].candidate, "qwen3-embedding-0.6b-cpu")
        self.assertEqual(metrics["recall_at_10"], 1.0)
        self.assertEqual(metrics["hit_at_1"], 1.0)
        self.assertEqual(metrics["questions_scored"], 2)
        self.assertEqual(metrics["questions_left_out"], 1)  # the scan question
        self.assertEqual(metrics["sources_not_in_any_chunk"], 0)
        self.assertEqual(metrics["batch_size"], 1)  # what the run used, so rows can be compared
        self.assertEqual(metrics["documents_skipped"], 1)  # the scan, so scores can be read fairly
        self.assertEqual(metrics["chunks"], 2)
        self.assertEqual(metrics["chunk_overlap_chars"], 100)  # the default; set by the caller
        self.assertEqual(metrics["dimensions"], 3)
        self.assertEqual(metrics["load_seconds"], 1.0)
        # 2 chunks in 1 second of fake time: 2 per second, so 7200 chunks take one hour.
        self.assertEqual(metrics["chunks_per_second"], 2.0)
        self.assertEqual(metrics["hours_for_vault_of_chunks"], 1.0)

    def test_a_question_whose_scan_has_a_readable_alternative_is_still_scored(self) -> None:
        inputs = corpus_inputs()
        saved = c.Question(
            "q6",
            "Who is the instructor?",
            "lookup",
            True,
            (c.Source("scan.pdf", "Dr. Example is the instructor"),),
            (c.Source("a.txt", "Dr. Example"),),
        )
        inputs = emb.EmbeddingInputs(inputs.corpus, inputs.documents, [saved])
        metrics = (
            emb.run_embedding_spike(FakeEmbedder(), inputs, environment=ENV, clock=Clock())
            .rows[0]
            .metrics
        )
        self.assertEqual(metrics["questions_scored"], 1)
        self.assertEqual(metrics["questions_left_out"], 0)
        self.assertEqual(metrics["recall_at_10"], 1.0)  # found through the alternative
        self.assertEqual(metrics["sources_not_in_any_chunk"], 0)  # the scan is not a typo

    def test_an_alternative_that_no_chunk_holds_is_counted_when_it_is_what_scores_the_question(
        self,
    ) -> None:
        # The scan is unreadable, so only the alternative could be found; it is mistyped, so the
        # question is an unavoidable miss that must show in the report and not only in the score.
        inputs = corpus_inputs()
        mistyped = c.Question(
            "q7",
            "Who is the instructor?",
            "lookup",
            True,
            (c.Source("scan.pdf", "Dr. Example is the instructor"),),
            (c.Source("a.txt", "Dr. Exmaple"),),
        )
        inputs = emb.EmbeddingInputs(inputs.corpus, inputs.documents, [mistyped])
        metrics = (
            emb.run_embedding_spike(FakeEmbedder(), inputs, environment=ENV, clock=Clock())
            .rows[0]
            .metrics
        )
        self.assertEqual(metrics["questions_scored"], 1)
        self.assertEqual(metrics["recall_at_10"], 0.0)
        self.assertEqual(metrics["sources_not_in_any_chunk"], 1)

    def test_the_right_document_with_the_wrong_passage_does_not_score(self) -> None:
        inputs = corpus_inputs()
        wrong = c.Question(
            "q5",
            "Who is the instructor?",
            "lookup",
            True,
            (c.Source("a.txt", "no such text"),),
            (),
            find_document=True,
        )
        inputs = emb.EmbeddingInputs(inputs.corpus, inputs.documents, [wrong])
        metrics = (
            emb.run_embedding_spike(FakeEmbedder(), inputs, environment=ENV, clock=Clock())
            .rows[0]
            .metrics
        )
        self.assertEqual(metrics["recall_at_10"], 0.0)
        self.assertEqual(metrics["sources_not_in_any_chunk"], 1)
        # Hit@1 asks only whether the top chunk comes from the right document, and here it does.
        self.assertEqual(metrics["hit_at_1"], 1.0)

    def test_the_report_says_whether_the_public_dataset_was_used(self) -> None:
        for public in (True, False):
            with self.subTest(public=public):
                report = emb.run_embedding_spike(
                    FakeEmbedder(),
                    corpus_inputs(),
                    public_dataset=public,
                    environment=ENV,
                    clock=Clock(),
                )
                self.assertIs(report.rows[0].metrics["public_dataset"], public)

    def test_the_default_is_the_public_dataset_label(self) -> None:
        report = emb.run_embedding_spike(
            FakeEmbedder(), corpus_inputs(), environment=ENV, clock=Clock()
        )
        self.assertIs(report.rows[0].metrics["public_dataset"], True)

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
    def __init__(
        self,
        texts: dict[str, str],
        broken: frozenset[str] = frozenset(),
        *,
        runs_in_process: bool = True,
    ) -> None:
        self.runs_in_process = runs_in_process
        # The name of the real engine this one stands in for, so its rows fit that row shape.
        self.name = "rapidocr-ppocrv5" if runs_in_process else "tesseract"
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

    def test_an_engine_that_runs_as_another_program_has_no_memory_figure(self) -> None:
        # This process's working set is the wrapper's when the engine is a separate program;
        # reporting it next to an in-process engine's real figure would mislead.
        outside = FakeOcr(
            {"a.png": "hello world", "b.png": "total 100", "c.png": "receipt"},
            runs_in_process=False,
        )
        with mock.patch.object(ocr, "peak_memory_mib", return_value=123.0):
            separate = ocr.run_ocr_spike(outside, self.samples(), environment=ENV, clock=Clock())
            inside = FakeOcr(
                {"a.png": "hello world", "b.png": "total 100", "c.png": "receipt"},
                runs_in_process=True,
            )
            local = ocr.run_ocr_spike(inside, self.samples(), environment=ENV, clock=Clock())
            plain = ocr.run_ocr_spike(
                FakeOcr({"a.png": "hello world", "b.png": "total 100", "c.png": "receipt"}),
                self.samples(),
                environment=ENV,
                clock=Clock(),
            )
        self.assertNotIn("peak_memory_mib", separate.rows[0].metrics)
        self.assertEqual(local.rows[0].metrics["peak_memory_mib"], 123.0)
        self.assertEqual(plain.rows[0].metrics["peak_memory_mib"], 123.0)  # in process by default
        self.assertFalse(adapters.TesseractEngine.runs_in_process)
        self.assertTrue(adapters.RapidOcrEngine.runs_in_process)

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

    def test_time_lost_in_a_failing_call_lowers_the_throughput(self) -> None:
        # The clock moves only inside a recognise call: 1 s for a good image, 100 s for a timeout.
        clock = Clock(step=0.0)

        class Slow(FakeOcr):
            def recognise(self, image: Path) -> str:
                clock.now += 100.0 if image.name in self.broken else 1.0
                return super().recognise(image)

        engine = Slow({"a.png": "hello world", "c.png": "receipt"}, broken=frozenset({"b.png"}))
        metrics = (
            ocr.run_ocr_spike(engine, self.samples(), environment=ENV, clock=clock).rows[0].metrics
        )
        self.assertEqual(metrics["failures"], 1)
        self.assertEqual(metrics["seconds_spent_in_failures"], 100.0)
        # Two images read in 102 s of work (the warm-up is not timed): 2 / 102 per second.
        self.assertEqual(metrics["images_per_minute"], round(2 / 102 * 60, 4))
        # Latency is still the latency of the images that worked.
        self.assertEqual(metrics["seconds_per_image_p50"], 1.0)

    def test_a_failed_image_counts_as_a_wrong_image_in_the_second_error_figure(self) -> None:
        engine = FakeOcr({"a.png": "hello world", "c.png": "receipt"}, broken=frozenset({"b.png"}))
        metrics = (
            ocr.run_ocr_spike(engine, self.samples(), environment=ENV, clock=Clock())
            .rows[0]
            .metrics
        )
        self.assertEqual(metrics["cer_mean"], 0.0)  # the two images that worked were exact
        self.assertEqual(metrics["cer_mean_failures_as_errors"], round(1 / 3, 4))

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
        self.assertEqual(metrics["images_per_minute"], 0.0)
        # Every call failed, which is the worst case and still has a figure: all wrong.
        self.assertEqual(metrics["cer_mean_failures_as_errors"], 1.0)

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

    def test_a_reference_saved_by_a_windows_tool_scores_as_its_text(self) -> None:
        # A byte order mark read as a character would add an error to every image.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.png").write_bytes(b"x")
            (root / "a.txt").write_bytes(codecs.BOM_UTF8 + b"alpha beta")
            (root / "b.png").write_bytes(b"x")
            (root / "b.txt").write_bytes(codecs.BOM_UTF16_LE + "gamma".encode("utf-16-le"))
            found = ocr.find_labelled_images(root)
        self.assertEqual([s.reference for s in found], ["alpha beta", "gamma"])
        engine = FakeOcr({"a.png": "alpha beta", "b.png": "gamma"})
        report = ocr.run_ocr_spike(engine, found, environment=ENV, clock=Clock())
        self.assertEqual(report.rows[0].metrics["cer_mean"], 0.0)


# --- pdf ------------------------------------------------------------------------------------


class FakePdf:
    def __init__(
        self,
        results: dict[str, pdf.PdfText | pdf.Outcome | Exception],
        *,
        detects_headings: bool = True,
        on_load: Callable[[], None] | None = None,
    ) -> None:
        self.results = results
        self.detects_headings = detects_headings
        # The name of the real extractor this one stands in for, so its rows fit its shape.
        self.name = "pypdfium2+pdfplumber" if detects_headings else "pypdfium2"
        self.on_load = on_load

    def load(self) -> None:
        if self.on_load is not None:
            self.on_load()

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
                "ok.pdf": pdf.PdfText(["page one", "", "page three"], ["A", "B", "C", "D"]),
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

    def test_a_missing_library_stops_the_run_and_is_not_a_failure_of_every_file(self) -> None:
        def missing() -> None:
            raise adapters.MissingDependencyError("pypdfium2 is not installed")

        extractor = FakePdf({"a.pdf": pdf.PdfText(["text"], [])}, on_load=missing)
        with (
            mock.patch.object(extractor, "extract") as extract,
            self.assertRaises(adapters.MissingDependencyError),
        ):
            pdf.run_pdf_spike(extractor, [Path("a.pdf")], environment=ENV, clock=Clock())
        extract.assert_not_called()

    def test_loading_the_library_is_not_timed_as_extraction(self) -> None:
        # The first import of pdfplumber takes seconds; inside the first file's time it would
        # dominate pages per second on a small set of PDFs.
        clock = Clock(step=1.0)

        def slow_import() -> None:
            clock.now += 100.0

        extractor = FakePdf({"a.pdf": pdf.PdfText(["one", "two"], [])}, on_load=slow_import)
        report = pdf.run_pdf_spike(extractor, [Path("a.pdf")], environment=ENV, clock=clock)
        self.assertEqual(report.rows[0].metrics["pages_per_second"], 2.0)

    def test_reference_files_saved_by_windows_tools_score_as_their_text(self) -> None:
        # With the mark read as a character, the first heading would never match and the first
        # word would always be wrong.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "doc.pdf").write_bytes(b"x")
            (root / "doc.headings.txt").write_bytes(
                codecs.BOM_UTF8 + b"Introduction\r\nMethods\r\n"
            )
            text = "Introduction to methods"
            (root / "doc.txt").write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
            extractor = FakePdf({"doc.pdf": pdf.PdfText([text], ["Introduction", "Methods"])})
            report = pdf.run_pdf_spike(
                extractor, [root / "doc.pdf"], environment=ENV, clock=Clock()
            )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["heading_recall"], 1.0)
        self.assertEqual(metrics["heading_precision"], 1.0)
        self.assertEqual(metrics["wer_mean"], 0.0)

    def test_an_unreadable_reference_stops_the_run_before_any_pdf_is_measured(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("a.pdf", "b.pdf"):
                (root / name).write_bytes(b"x")
            (root / "b.headings.txt").write_bytes(b"\xff\xfd not text")
            extractor = FakePdf({"a.pdf": pdf.PdfText(["a"], []), "b.pdf": pdf.PdfText(["b"], [])})
            with (
                mock.patch.object(extractor, "extract") as extract,
                self.assertRaises(c.SpikeInputError) as caught,
            ):
                pdf.run_pdf_spike(
                    extractor, [root / "a.pdf", root / "b.pdf"], environment=ENV, clock=Clock()
                )
        extract.assert_not_called()
        self.assertIn("a headings file is not text this script can read", str(caught.exception))

    def test_word_error_rate_against_a_reference_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ok.pdf").write_bytes(b"x")
            (root / "ok.txt").write_text("the quick brown fox", encoding="utf-8")
            (root / "empty.pdf").write_bytes(b"x")
            (root / "empty.txt").write_text("  ", encoding="utf-8")
            extractor = FakePdf(
                {
                    "ok.pdf": pdf.PdfText(["the quick brown dog"], []),
                    "empty.pdf": pdf.PdfText(["text"], []),
                }
            )
            report = pdf.run_pdf_spike(
                extractor, [root / "ok.pdf", root / "empty.pdf"], environment=ENV, clock=Clock()
            )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["references_used"], 1)
        self.assertEqual(metrics["references_unusable"], 1)
        self.assertEqual(metrics["wer_mean"], 0.25)

    def test_a_reference_with_no_words_is_counted_and_its_pdf_still_measured(self) -> None:
        """An empty or blank reference has no error rate. It is counted in
        ``references_unusable`` whatever its PDF's outcome, so a mistyped file cannot take a
        sample out of the word error rates unseen, and its PDF is still extracted and timed."""
        names = ("ok", "empty", "blank", "broken")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in names:
                (root / f"{name}.pdf").write_bytes(b"x")
            (root / "ok.txt").write_text("the quick brown fox", encoding="utf-8")
            (root / "empty.txt").write_bytes(b"")
            (root / "blank.txt").write_bytes(codecs.BOM_UTF8 + b" \r\n\t\r\n")
            (root / "broken.txt").write_text("\n", encoding="utf-8")
            extractor = FakePdf(
                {
                    "ok.pdf": pdf.PdfText(["the quick brown fox"], []),
                    "empty.pdf": pdf.PdfText(["one", "two"], []),
                    "blank.pdf": pdf.PdfText(["three"], []),
                    "broken.pdf": pdf.Outcome.CORRUPT,
                },
                detects_headings=False,
            )
            report = pdf.run_pdf_spike(
                extractor,
                [root / f"{name}.pdf" for name in names],
                environment=ENV,
                clock=Clock(),
            )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["references_used"], 1)
        self.assertEqual(metrics["references_unusable"], 3)
        self.assertEqual(metrics["wer_mean"], 0.0)
        self.assertEqual((metrics["files_ok"], metrics["files_corrupt"]), (3, 1))
        self.assertEqual(metrics["pages"], 4)  # the PDFs with a blank reference were read

    def heading_run(
        self, detected: list[str], expected: str | None, *, detects_headings: bool = True
    ) -> dict[str, float]:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "doc.pdf").write_bytes(b"x")
            if expected is not None:
                (root / "doc.headings.txt").write_text(expected, encoding="utf-8")
            extractor = FakePdf(
                {"doc.pdf": pdf.PdfText(["page"], detected)}, detects_headings=detects_headings
            )
            report = pdf.run_pdf_spike(
                extractor, [root / "doc.pdf"], environment=ENV, clock=Clock()
            )
        return dict(report.rows[0].metrics)

    def test_heading_accuracy_is_scored_against_the_expected_headings(self) -> None:
        # Two real headings found, one false positive, one real heading missed.
        metrics = self.heading_run(
            ["Introduction", "  METHODS  ", "A bold sentence"], "introduction\nMethods\nResults\n\n"
        )
        self.assertEqual(metrics["files_with_heading_reference"], 1)
        self.assertEqual(metrics["heading_precision"], round(2 / 3, 4))
        self.assertEqual(metrics["heading_recall"], round(2 / 3, 4))
        self.assertEqual(metrics["heading_f1"], round(2 / 3, 4))

    def test_a_false_heading_repeated_on_every_page_counts_every_time(self) -> None:
        # A running header found on 20 pages is 20 false positives, not one, and the one real
        # heading found once is still just one true positive.
        metrics = self.heading_run(["Introduction"] + ["ACME CONFIDENTIAL"] * 20, "Introduction")
        self.assertEqual(metrics["heading_false_positives"], 20)
        self.assertEqual(metrics["heading_precision"], round(1 / 21, 4))
        self.assertEqual(metrics["heading_recall"], 1.0)

    def test_a_heading_found_more_often_than_it_is_expected_does_not_score_twice(self) -> None:
        metrics = self.heading_run(["One", "One", "One"], "One")
        self.assertEqual(metrics["heading_false_positives"], 2)
        self.assertEqual(metrics["heading_precision"], round(1 / 3, 4))
        self.assertEqual(metrics["heading_recall"], 1.0)

    def test_a_heading_expected_twice_and_found_once_is_half_found(self) -> None:
        metrics = self.heading_run(["Summary"], "Summary\nSummary")
        self.assertEqual(metrics["heading_precision"], 1.0)
        self.assertEqual(metrics["heading_recall"], 0.5)

    def test_the_same_count_of_candidates_can_be_all_wrong(self) -> None:
        # Counting candidates cannot tell this from a perfect run; accuracy can.
        right = self.heading_run(["One", "Two"], "One\nTwo")
        wrong = self.heading_run(["Not", "Headings"], "One\nTwo")
        self.assertEqual(right["heading_candidates"], wrong["heading_candidates"])
        self.assertEqual(right["heading_f1"], 1.0)
        self.assertEqual(wrong["heading_recall"], 0.0)
        self.assertEqual(wrong["heading_precision"], 0.0)
        self.assertEqual(wrong["heading_f1"], 0.0)

    def test_finding_no_headings_scores_zero_recall(self) -> None:
        metrics = self.heading_run([], "One\nTwo")
        self.assertEqual(metrics["heading_recall"], 0.0)
        self.assertEqual(metrics["heading_f1"], 0.0)
        self.assertNotIn("heading_precision", metrics)  # nothing was detected to be right or wrong

    def test_without_a_reference_file_no_accuracy_is_claimed(self) -> None:
        metrics = self.heading_run(["One"], None)
        self.assertNotIn("heading_recall", metrics)
        self.assertNotIn("files_with_heading_reference", metrics)

    def test_a_heading_free_pdf_is_a_label_and_false_headings_in_it_count(self) -> None:
        # An empty headings file says the PDF has no headings, so all three are false.
        metrics = self.heading_run(["Looks like", "a heading", "but is not"], "  \n\n")
        self.assertEqual(metrics["files_with_heading_reference"], 1)
        self.assertEqual(metrics["heading_false_positives"], 3)
        self.assertEqual(metrics["heading_precision"], 0.0)
        self.assertEqual(metrics["heading_f1"], 0.0)
        self.assertNotIn("heading_recall", metrics)  # no expected heading to find

    def test_finding_nothing_in_a_heading_free_pdf_is_correct_and_claims_no_ratio(self) -> None:
        metrics = self.heading_run([], "")
        self.assertEqual(metrics["files_with_heading_reference"], 1)
        self.assertEqual(metrics["heading_false_positives"], 0)
        for name in ("heading_precision", "heading_recall", "heading_f1"):
            self.assertNotIn(name, metrics)

    def test_false_headings_in_a_heading_free_pdf_lower_the_precision_of_the_whole_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, expected in (("a", "One\nTwo"), ("b", "")):
                (root / f"{name}.pdf").write_bytes(b"x")
                (root / f"{name}.headings.txt").write_text(expected, encoding="utf-8")
            extractor = FakePdf(
                {
                    "a.pdf": pdf.PdfText(["page"], ["One", "Two"]),
                    "b.pdf": pdf.PdfText(["page"], ["Noise", "More noise"]),
                }
            )
            report = pdf.run_pdf_spike(
                extractor, [root / "a.pdf", root / "b.pdf"], environment=ENV, clock=Clock()
            )
        metrics = report.rows[0].metrics
        self.assertEqual(metrics["heading_recall"], 1.0)
        self.assertEqual(metrics["heading_precision"], 0.5)  # 2 right of 4 found
        self.assertEqual(metrics["heading_false_positives"], 2)

    def test_an_extractor_that_detects_no_headings_is_not_scored_for_them(self) -> None:
        metrics = self.heading_run([], "One\nTwo", detects_headings=False)
        self.assertNotIn("heading_recall", metrics)

    def test_heading_text_never_reaches_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "doc.pdf").write_bytes(b"x")
            (root / "doc.headings.txt").write_text(PRIVATE_TEXT, encoding="utf-8")
            extractor = FakePdf({"doc.pdf": pdf.PdfText(["page"], [PRIVATE_TEXT, "other"])})
            report = pdf.run_pdf_spike(
                extractor, [root / "doc.pdf"], environment=ENV, clock=Clock()
            )
        self.assertNotIn(PRIVATE_TEXT, everything(report))

    def test_no_pdfs_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            pdf.run_pdf_spike(FakePdf({}), [], environment=ENV)


if __name__ == "__main__":
    unittest.main()

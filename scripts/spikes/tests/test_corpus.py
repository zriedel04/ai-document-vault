from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import ClassVar
from xml.sax.saxutils import escape

from vault_spikes import corpus as c
from vault_spikes import retrieval as rv

QUESTIONS = """
questions:
- id: q1
  question: Who teaches the course?
  category: lookup
  find_document: true
  answerable: true
  answer: Someone.
  sources:
  - path: a/syllabus.txt
    page: null
    quote: Instructor
  alternatives:
  - path: a/copy.txt
    quote: Instructor
- id: q2
  question: Unanswerable?
  category: not_in_vault
  answerable: false
  answer: Not in the vault.
  sources: []
"""
MANIFEST = {
    "documents": [
        {"path": "a/syllabus.txt", "kind": "text", "pages": None, "needs_ocr": False},
        {
            "path": "a/copy.txt",
            "kind": "text",
            "pages": None,
            "needs_ocr": False,
            "exact_duplicate_of": "a/syllabus.txt",
        },
        {"path": "a/scan.pdf", "kind": "pdf_scan", "pages": 2, "needs_ocr": True},
        {"path": "a/notes.docx", "kind": "docx", "pages": None, "needs_ocr": False},
        {"path": "a/book.pdf", "kind": "pdf_text", "pages": 1, "needs_ocr": False},
    ]
}


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


class LoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_questions_are_read(self) -> None:
        questions = c.load_questions(self.write("q.yaml", QUESTIONS))
        self.assertEqual([q.id for q in questions], ["q1", "q2"])
        self.assertEqual(questions[0].sources, (c.Source("a/syllabus.txt", "Instructor"),))
        self.assertEqual(questions[0].alternatives, (c.Source("a/copy.txt", "Instructor"),))
        self.assertEqual(questions[0].source_paths, ("a/syllabus.txt",))
        self.assertFalse(questions[1].answerable)
        self.assertEqual([q.find_document for q in questions], [True, False])  # false unless marked

    def test_malformed_question_files_are_input_errors(self) -> None:
        bad = {
            "missing": None,
            "not a mapping": "- a\n- b\n",
            "no list": "questions: 5\n",
            "empty list": "questions: []\n",
            "bad yaml": "questions: [unclosed\n",
            "no id": "questions:\n- question: q\n  category: c\n  answerable: true\n",
            "answerable text": (
                "questions:\n- {id: a, question: q, category: c, answerable: 'yes'}\n"
            ),
            "no sources": "questions:\n- {id: a, question: q, category: c, answerable: true}\n",
            "source without path": (
                "questions:\n- {id: a, question: q, category: c, answerable: true,"
                " sources: [{page: 1}]}\n"
            ),
            "source without quote": (
                "questions:\n- {id: a, question: q, category: c, answerable: true,"
                " sources: [{path: a.txt, page: 1}]}\n"
            ),
            "source with a blank quote": (
                "questions:\n- {id: a, question: q, category: c, answerable: true,"
                " sources: [{path: a.txt, quote: '  '}]}\n"
            ),
            "alternative without quote": (
                "questions:\n- {id: a, question: q, category: c, answerable: true,"
                " sources: [{path: a.txt, quote: x}], alternatives: [{path: b.txt}]}\n"
            ),
            "sources not a list": (
                "questions:\n- {id: a, question: q, category: c, answerable: true, sources: 3}\n"
            ),
        }
        two = "questions:\n- {id: a, question: q, category: c, answerable: false}\n"
        bad["two questions with one id"] = two + two.split("\n", 1)[1]
        bad["an empty id"] = "questions:\n- {id: '', question: q, category: c, answerable: false}\n"
        bad["a blank id"] = (
            "questions:\n- {id: '  ', question: q, category: c, answerable: false}\n"
        )
        one = "questions:\n- {id: a, question: q, category: c, answerable: true, "
        bad["find_document as text"] = (
            one + "find_document: 'yes', sources: [{path: a.txt, quote: x}]}\n"
        )
        bad["find_document without an answer"] = (
            "questions:\n- {id: a, question: q, category: c, answerable: false,"
            " find_document: true}\n"
        )
        bad["find_document with two documents"] = (
            one
            + "find_document: true, sources: [{path: a.txt, quote: x}, {path: b.txt, quote: y}]}\n"
        )
        for name, text in bad.items():
            with self.subTest(name):
                path = self.root / "bad.yaml"
                if text is None:
                    path = self.root / "does-not-exist.yaml"
                else:
                    path.write_text(text, encoding="utf-8")
                with self.assertRaises(c.SpikeInputError):
                    c.load_questions(path)

    def test_manifest_and_duplicates(self) -> None:
        documents = c.load_manifest(self.write("m.json", json.dumps(MANIFEST)))
        self.assertEqual(len(documents), 5)
        canonical = c.canonical_paths(documents)
        self.assertEqual(canonical["a/copy.txt"], "a/syllabus.txt")
        self.assertEqual(canonical["a/syllabus.txt"], "a/syllabus.txt")

    def test_malformed_manifests_are_input_errors(self) -> None:
        for name, text in {
            "not json": "{",
            "not a mapping": "[]",
            "no documents": "{}",
            "no path": '{"documents": [{"kind": "text"}]}',
        }.items():
            with self.subTest(name), self.assertRaises(c.SpikeInputError):
                c.load_manifest(self.write("bad.json", text))
        with self.assertRaises(c.SpikeInputError):
            c.load_manifest(self.root / "missing.json")


class ReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "a").mkdir()
        (self.root / "a" / "syllabus.txt").write_text(
            "Instructor: Dr. Example. " * 80, encoding="utf-8"
        )
        (self.root / "a" / "copy.txt").write_text("Instructor: Dr. Example.", encoding="utf-8")
        (self.root / "a" / "scan.pdf").write_bytes(b"%PDF-1.4")
        (self.root / "a" / "book.pdf").write_bytes(b"%PDF-1.4")
        write_docx(self.root / "a" / "notes.docx", ["First & second", "Third <one>", "  "])
        self.documents = c.load_manifest(self.write_manifest())

    def write_manifest(self) -> Path:
        path = self.root / "manifest.json"
        path.write_text(json.dumps(MANIFEST), encoding="utf-8")
        return path

    def test_text_docx_and_pdf_are_read_and_scans_are_skipped(self) -> None:
        corpus = c.build_corpus(self.root, self.documents, lambda _path: ["page one", "page two"])
        self.assertEqual(corpus.skipped, 1)  # the scan
        self.assertIn("a/book.pdf", corpus.readable)
        self.assertNotIn("a/scan.pdf", corpus.readable)
        docx_chunks = [x for x in corpus.chunks if x.doc_path == "a/notes.docx"]
        self.assertEqual(docx_chunks[0].text, "First & second\nThird <one>")
        pdf_pages = [x.page for x in corpus.chunks if x.doc_path == "a/book.pdf"]
        self.assertEqual(pdf_pages, [1, 2])

    def test_without_a_pdf_reader_pdfs_are_skipped_and_counted(self) -> None:
        corpus = c.build_corpus(self.root, self.documents)
        self.assertEqual(corpus.skipped, 2)

    def test_an_exact_duplicate_is_not_chunked_but_can_still_be_cited(self) -> None:
        corpus = c.build_corpus(self.root, self.documents, lambda _path: ["page"])
        self.assertEqual([x for x in corpus.chunks if x.doc_path == "a/copy.txt"], [])
        self.assertTrue([x for x in corpus.chunks if x.doc_path == "a/syllabus.txt"])
        self.assertIn("a/copy.txt", corpus.readable)
        texts = [x.text for x in corpus.chunks]
        self.assertEqual(len(texts), len(set(texts)), "no passage is embedded twice")

    def test_a_duplicate_of_an_unreadable_original_is_unreadable_and_counted(self) -> None:
        documents = [
            replace(d, needs_ocr=True) if d.path == "a/syllabus.txt" else d for d in self.documents
        ]
        corpus = c.build_corpus(self.root, documents, lambda _path: ["page"])
        self.assertNotIn("a/syllabus.txt", corpus.readable)
        self.assertNotIn("a/copy.txt", corpus.readable)
        self.assertEqual(corpus.skipped, 3)  # the scan, the original and its copy

    def test_a_listed_duplicate_that_is_missing_is_still_refused(self) -> None:
        (self.root / "a" / "copy.txt").unlink()
        with self.assertRaises(c.SpikeInputError):
            c.build_corpus(self.root, self.documents, lambda _path: ["page"])

    def test_duplicates_resolve_to_one_original_however_they_are_listed(self) -> None:
        def doc(path: str, original: str | None) -> c.Document:
            return c.Document(path, "text", None, False, original)

        chain = [doc("c.txt", "b.txt"), doc("b.txt", "a.txt"), doc("a.txt", None)]
        self.assertEqual(set(c.canonical_paths(chain).values()), {"a.txt"})
        unknown = [doc("x.txt", "not-listed.txt")]
        self.assertEqual(c.canonical_paths(unknown), {"x.txt": "x.txt"})
        cycle = [doc("p.txt", "q.txt"), doc("q.txt", "p.txt")]
        self.assertEqual(c.canonical_paths(cycle), {"p.txt": "p.txt", "q.txt": "q.txt"})
        self.assertEqual(c.canonical_paths([doc("s.txt", "s.txt")]), {"s.txt": "s.txt"})

    def test_a_listed_path_cannot_leave_the_corpus_folder(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        for relative in ("../outside.txt", "a/../../outside.txt", "missing.txt", "a"):
            with self.subTest(relative), self.assertRaises(c.SpikeInputError):
                c.safe_file(self.root, relative)

    def test_a_link_out_of_the_corpus_is_refused(self) -> None:
        outside = self.root.parent / "outside2.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        try:
            (self.root / "a" / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("cannot create symbolic links here")
        with self.assertRaises(c.SpikeInputError):
            c.safe_file(self.root, "a/link.txt")

    def docx_document(self, name: str) -> c.Document:
        return c.Document(f"a/{name}", "docx", None, False, None)

    def test_a_huge_docx_part_is_skipped(self) -> None:
        path = self.root / "a" / "big.docx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"a" * (c.MAX_FILE_BYTES + 1))
        self.assertIsNone(c.read_pages(path, self.docx_document("big.docx")))

    def test_a_docx_that_cannot_be_read_is_skipped_not_fatal(self) -> None:
        corrupt = self.root / "a" / "corrupt.docx"
        corrupt.write_bytes(b"not a zip file at all")
        no_body = self.root / "a" / "nobody.docx"
        with zipfile.ZipFile(no_body, "w") as archive:
            archive.writestr("word/other.xml", "<x/>")
        for name in ("corrupt.docx", "nobody.docx"):
            with self.subTest(name):
                self.assertIsNone(c.read_pages(self.root / "a" / name, self.docx_document(name)))

    def test_one_unreadable_document_does_not_stop_the_corpus(self) -> None:
        (self.root / "a" / "syllabus.txt").write_text("fine " * 10, encoding="utf-8")
        (self.root / "a" / "notes.docx").write_bytes(b"corrupt")
        corpus = c.build_corpus(self.root, self.documents, lambda _path: None)
        self.assertIn("a/syllabus.txt", corpus.readable)
        self.assertNotIn("a/notes.docx", corpus.readable)
        self.assertNotIn("a/book.pdf", corpus.readable)  # the reader gave up on it
        self.assertEqual(corpus.skipped, 3)  # scan, book.pdf and notes.docx


class SplitTest(unittest.TestCase):
    def test_windows_are_bounded_and_overlap(self) -> None:
        text = " ".join(f"word{i}" for i in range(200))
        pieces = c.split_text(text, max_chars=100, overlap=20)
        self.assertGreater(len(pieces), 5)
        self.assertTrue(all(len(p) <= 100 for p in pieces))
        self.assertEqual(pieces[0].split()[0], "word0")
        self.assertIn(text.split()[-1], pieces[-1])
        # every word appears in at least one piece
        joined = " ".join(pieces)
        self.assertTrue(all(f"word{i}" in joined for i in range(200)))

    def test_short_and_empty_text(self) -> None:
        self.assertEqual(c.split_text("short"), ["short"])
        self.assertEqual(c.split_text("  \n "), [])

    def test_text_without_spaces_still_ends(self) -> None:
        self.assertEqual(len(c.split_text("x" * 250, max_chars=100, overlap=0)), 3)

    def test_bad_sizes_are_errors(self) -> None:
        for max_chars, overlap in ((0, 0), (10, 10), (10, -1)):
            with self.subTest(max_chars=max_chars), self.assertRaises(ValueError):
                c.split_text("text", max_chars, overlap)


def src(path: str, quote: str = "the passage") -> c.Source:
    return c.Source(path, quote)


def chunk(doc: str, text: str = "nothing relevant", page: int = 1) -> c.Chunk:
    return c.Chunk(doc, page, text)


def question(
    sources: tuple[c.Source, ...],
    alternatives: tuple[c.Source, ...] = (),
    answerable: bool = True,
    find_document: bool = True,
) -> c.Question:
    return c.Question("q", "text", "lookup", answerable, sources, alternatives, find_document)


HIT = "the passage that answers it"
ORIG = src("orig", "the passage that answers it")


class RetrievalScoreTest(unittest.TestCase):
    CANON: ClassVar[dict[str, str]] = {"copy": "orig"}

    def score(self, q: c.Question, ranked: list[c.Chunk]) -> tuple[float, float]:
        return rv.score_question(q, ranked, self.CANON)

    def test_a_hit_at_rank_one(self) -> None:
        self.assertEqual(
            self.score(question((ORIG,)), [chunk("orig", f"see {HIT} here"), chunk("x")]),
            (1.0, 1.0),
        )

    def test_rank_decides_the_reciprocal_rank(self) -> None:
        ranked = [chunk("x"), chunk("y"), chunk("orig", HIT)]
        self.assertEqual(self.score(question((ORIG,)), ranked), (1.0, 1 / 3))

    def test_a_miss_and_the_cut_off(self) -> None:
        self.assertEqual(self.score(question((ORIG,)), [chunk("x"), chunk("y")]), (0.0, 0.0))
        ranked = [chunk(f"x{i}") for i in range(10)] + [chunk("orig", HIT)]
        self.assertEqual(self.score(question((ORIG,)), ranked), (0.0, 0.0))

    def test_the_right_document_but_another_passage_scores_nothing(self) -> None:
        # The top ten can all come from the right document and still miss the passage.
        ranked = [chunk("orig", f"an unrelated paragraph number {i}") for i in range(10)]
        self.assertEqual(self.score(question((ORIG,)), ranked), (0.0, 0.0))

    def test_the_passage_in_the_wrong_document_scores_nothing(self) -> None:
        self.assertEqual(self.score(question((ORIG,)), [chunk("other", HIT)]), (0.0, 0.0))

    def test_whitespace_and_line_breaks_in_the_quote_or_chunk_are_ignored(self) -> None:
        q = question((src("orig", "the passage\nthat   answers\tit"),))
        self.assertEqual(self.score(q, [chunk("orig", f"x  {HIT}\n y")]), (1.0, 1.0))

    def test_exact_duplicates_are_interchangeable(self) -> None:
        self.assertEqual(self.score(question((ORIG,)), [chunk("copy", HIT)]), (1.0, 1.0))

    def test_an_alternative_counts_for_a_single_passage_question(self) -> None:
        q = question((ORIG,), (src("alt", "another way to say it"),))
        self.assertEqual(self.score(q, [chunk("alt", "here is another way to say it")]), (1.0, 1.0))

    def test_a_multi_passage_question_needs_every_source(self) -> None:
        a, b = src("a", "first passage"), src("b", "second passage")
        q = question((a, b), (src("alt", "alternative passage"),))
        recall, rr = self.score(q, [chunk("a", "first passage"), chunk("x")])
        self.assertEqual((recall, rr), (0.5, 1.0))
        both = [chunk("a", "first passage"), chunk("b", "second passage")]
        self.assertEqual(self.score(q, both)[0], 1.0)
        # an alternative does not stand in for a passage of a multi-passage question
        self.assertEqual(self.score(q, [chunk("alt", "alternative passage")])[0], 0.0)

    def test_two_passages_of_one_document_are_two_sources(self) -> None:
        q = question((src("a", "first passage"), src("a", "second passage")))
        self.assertEqual(self.score(q, [chunk("a", "first passage only")])[0], 0.5)

    def test_half_a_quote_is_not_the_passage(self) -> None:
        # A chunk with the generic start of a sentence, or with its end, does not hold the passage.
        quote = "the project report is due on the first Monday of December at noon"
        q = question((src("a", quote),))
        self.assertEqual(
            self.score(q, [chunk("a", "the project report is due on the first")])[0], 0.0
        )
        self.assertEqual(self.score(q, [chunk("a", "first Monday of December at noon")])[0], 0.0)
        self.assertEqual(self.score(q, [chunk("a", f"see: {quote}.")]), (1.0, 1.0))

    def test_two_halves_in_two_chunks_do_not_stand_in_for_the_whole_either(self) -> None:
        quote = "one two three four five six seven eight nine ten"
        q = question((src("a", quote),), (src("alt", "an alternative way of saying it"),))
        halves = [
            chunk("a", "one two three four five six"),
            chunk("a", "five six seven eight nine ten"),
        ]
        self.assertEqual(self.score(q, halves), (0.0, 0.0))

    def test_a_short_quote_must_be_whole(self) -> None:
        q = question((src("a", "Total $187.92"),))
        self.assertEqual(self.score(q, [chunk("a", "Total only")])[0], 0.0)

    def test_hit_at_1_is_by_document_not_by_passage(self) -> None:
        # The top chunk comes from the right document but not from the quoted passage: Recall and
        # MRR score a miss, Hit@1 (find the document) a hit.
        q = question((ORIG,))
        scores = rv.score_all([q], {"q": [chunk("orig", "an unrelated paragraph")]}, self.CANON)
        self.assertEqual((scores.recall_at_10, scores.mrr_at_10, scores.hit_at_1), (0.0, 0.0, 1.0))

    def test_hit_at_1_is_wrong_when_the_top_chunk_is_from_another_document(self) -> None:
        q = question((ORIG,))
        scores = rv.score_all([q], {"q": [chunk("x", HIT), chunk("orig", HIT)]}, self.CANON)
        self.assertEqual((scores.hit_at_1, scores.mrr_at_10), (0.0, 0.5))

    def test_hit_at_1_accepts_a_duplicate_and_an_alternative_document(self) -> None:
        self.assertEqual(
            rv.score_all([question((ORIG,))], {"q": [chunk("copy")]}, self.CANON).hit_at_1, 1.0
        )
        alt = question((ORIG,), (src("alt", "another way"),))
        self.assertEqual(rv.score_all([alt], {"q": [chunk("alt")]}, self.CANON).hit_at_1, 1.0)

    def test_hit_at_1_covers_only_find_the_document_questions_with_one_document(self) -> None:
        single = c.Question("s", "t", "lookup", True, (src("a", "alpha"),), (), True)
        several_passages = c.Question(
            "p", "t", "x", True, (src("a", "alpha"), src("a", "beta")), (), True
        )
        several_documents = c.Question("m", "t", "multi_doc", True, (src("a"), src("b")), (), True)
        ranked = {q.id: [chunk("a")] for q in (single, several_passages, several_documents)}
        ranked["s"] = [chunk("zzz")]  # the single-document question is the one that misses
        scores = rv.score_all([single, several_passages, several_documents], ranked, {})
        # Hit@1 averages the two questions that have one document to find; "m" does not count.
        self.assertEqual(
            (scores.hit_at_1, scores.hit_at_1_questions, scores.questions), (0.5, 2, 3)
        )

    def test_hit_at_1_is_none_when_no_question_has_one_document(self) -> None:
        q = c.Question("m", "t", "multi_doc", True, (src("a"), src("b")), (), True)
        scores = rv.score_all([q], {"m": [chunk("a")]}, {})
        self.assertIsNone(scores.hit_at_1)
        self.assertEqual(scores.hit_at_1_questions, 0)

    def test_an_ordinary_question_with_one_document_is_not_scored_by_hit_at_1(self) -> None:
        # "Who is the instructor?" has its evidence in one document but is not a question about
        # finding a document, so it counts for Recall and MRR only.
        ordinary = question((ORIG,), find_document=False)
        scores = rv.score_all([ordinary], {"q": [chunk("orig")]}, self.CANON)
        self.assertEqual(scores.questions, 1)
        self.assertIsNone(scores.hit_at_1)
        self.assertEqual(scores.hit_at_1_questions, 0)

    def test_an_empty_ranking_is_a_miss(self) -> None:
        self.assertEqual(rv.score_all([question((ORIG,))], {"q": []}, {}).hit_at_1, 0.0)

    def test_averages_skip_questions_without_a_ranking(self) -> None:
        q1 = c.Question("q1", "t", "lookup", True, (src("a", "alpha"),), ())
        q2 = c.Question("q2", "t", "lookup", True, (src("b", "beta"),), ())
        scores = rv.score_all([q1, q2], {"q1": [chunk("a", "alpha")]}, {})
        self.assertEqual(scores.questions, 1)
        self.assertEqual(scores.recall_at_10, 1.0)
        with self.assertRaises(ValueError):
            rv.score_all([q2], {}, {})

    def test_sources_no_chunk_holds_are_counted(self) -> None:
        q = question((src("a", "alpha"), src("b", "beta"), src("c", "gamma")))
        chunks = [chunk("a", "alpha here"), chunk("c", "something else")]
        readable = frozenset({"a", "b", "c"})
        self.assertEqual(rv.sources_missing_from([q], chunks, {}, readable), 2)

    def test_a_source_in_a_document_the_run_could_not_read_is_not_counted_as_missing(self) -> None:
        # That is the reader's limit (a scan), not a mistyped quote or a cut boundary.
        q = question((src("scan", "alpha"),), (src("a", "alpha"),))
        chunks = [chunk("a", "alpha here")]
        self.assertEqual(rv.sources_missing_from([q], chunks, {}, frozenset({"a"})), 0)
        self.assertEqual(rv.sources_missing_from([q], chunks, {}, frozenset({"a", "scan"})), 1)

    def test_a_question_can_be_scored_when_all_sources_or_a_lone_sources_alternative_is_readable(
        self,
    ) -> None:
        one = question((src("scan"),), (src("alt"),))
        self.assertTrue(rv.can_be_scored(one, frozenset({"scan"})))  # the primary is enough
        self.assertTrue(rv.can_be_scored(one, frozenset({"alt"})))  # so is the alternative
        self.assertFalse(rv.can_be_scored(one, frozenset({"other"})))
        two = question((src("a"), src("scan")), (src("alt"),))
        self.assertTrue(rv.can_be_scored(two, frozenset({"a", "scan"})))
        # With two required passages an alternative does not stand in for the unreadable one,
        # as score_question gives it that power only for a lone required passage.
        self.assertFalse(rv.can_be_scored(two, frozenset({"a", "alt"})))
        self.assertFalse(rv.can_be_scored(question((), (src("alt"),)), frozenset({"alt"})))
        self.assertFalse(
            rv.can_be_scored(question((src("a"),), answerable=False), frozenset({"a"}))
        )

    def test_ranking_by_similarity(self) -> None:
        vectors = [[1.0, 0.0], [0.0, 1.0], [0.9, 0.1]]
        chunks = [chunk("d0"), chunk("d1"), chunk("d2")]
        ranked = rv.rank_by_similarity([1.0, 0.0], vectors, chunks, k=2)
        self.assertEqual([x.doc_path for x in ranked], ["d0", "d2"])


class OverlapTest(unittest.TestCase):
    @staticmethod
    def with_quotes(*lengths: int, alternative: bool = False) -> list[c.Question]:
        sources = tuple(src("a", "x" * length) for length in lengths)
        if alternative:
            return [question((src("a", "short"),), sources)]
        return [question(sources)] if sources else []

    def test_the_overlap_follows_the_longest_quote_within_limits(self) -> None:
        self.assertEqual(c.overlap_for([]), c.DEFAULT_OVERLAP)
        self.assertEqual(c.overlap_for(self.with_quotes(40, 60)), c.DEFAULT_OVERLAP)
        self.assertEqual(c.overlap_for(self.with_quotes(40, 200)), 251)  # a quarter more, plus one
        self.assertEqual(c.overlap_for(self.with_quotes(200, alternative=True)), 251)
        self.assertEqual(c.overlap_for(self.with_quotes(5000)), c.DEFAULT_MAX_CHARS // 2)
        self.assertEqual(c.overlap_for(self.with_quotes(5000), max_chars=100), 50)

    def test_whitespace_in_a_quote_counts_once(self) -> None:
        spaced = [question((src("a", "word   " * 40),))]  # 40 words: 199 characters when joined
        self.assertEqual(c.overlap_for(spaced), 250)

    def test_no_quote_is_cut_between_chunks_with_that_overlap_but_is_with_the_default(self) -> None:
        words = [f"w{i:03d}" for i in range(600)]
        text = " ".join(words)
        size = 34  # about 170 characters, more than the default overlap of 100
        quotes = [" ".join(words[i : i + size]) for i in range(len(words) - size)]
        questions = [question((src("a", quotes[0]),))]
        overlap = c.overlap_for([question((src("a", quotes[0]),))])
        wide = c.split_text(text, overlap=overlap)
        narrow = c.split_text(text)
        self.assertEqual(len(questions), 1)
        self.assertTrue(all(any(q in piece for piece in wide) for q in quotes))
        self.assertFalse(all(any(q in piece for piece in narrow) for q in quotes))

    def test_the_embedding_command_uses_the_overlap_for_the_quotes(self) -> None:
        # The metric that records it is part of what the spike may report.
        from vault_spikes import spike_embedding

        self.assertIn("chunk_overlap_chars", spike_embedding.METRICS)


if __name__ == "__main__":
    unittest.main()

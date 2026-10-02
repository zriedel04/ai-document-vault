"""Checks that the generated corpus and eval set are consistent with each other and with the rules.

Run from demo/generate: ``uv run python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from PIL import Image

import build
from model import Document
from questions import build_questions
from render import Pen, image_layout, scan_layout

DOCS = {d.path: d for d in build.all_documents()}
FILES = build.build_outputs()
QUESTIONS = build_questions()
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def norm(text: str) -> str:
    return " ".join(text.split())


def numbers(text: str) -> set[str]:
    """Every number in ``text``: dates, years, counts and times such as ``11:59``."""
    return set(re.findall(r"\d+(?::\d+)?", text))


def data(doc: Document) -> bytes:
    return FILES[f"demo/corpus/{doc.path}"]


class CorpusTests(unittest.TestCase):
    def test_corpus_size_matches_the_plan(self) -> None:
        # docs/architecture.md section 21: about 40-60 documents.
        self.assertTrue(40 <= len(DOCS) <= 60, len(DOCS))

    def test_every_format_in_the_plan_is_present(self) -> None:
        kinds = {d.kind for d in DOCS.values()}
        self.assertEqual(kinds, {"pdf_text", "pdf_scan", "docx", "txt", "md", "csv", "png", "jpg"})

    def test_paths_are_relative_and_clean(self) -> None:
        for path in DOCS:
            self.assertFalse(path.startswith("/"), path)
            self.assertNotIn("..", path)
            self.assertEqual(path, path.lower(), "lower-case paths keep Windows and Linux in step")

    def test_exact_duplicates_are_byte_identical(self) -> None:
        found = 0
        for doc in DOCS.values():
            if doc.exact_duplicate_of:
                found += 1
                self.assertEqual(data(doc), data(DOCS[doc.exact_duplicate_of]), doc.path)
        self.assertGreaterEqual(found, 2)

    def test_no_other_documents_are_byte_identical(self) -> None:
        seen: dict[str, str] = {}
        for doc in DOCS.values():
            if doc.exact_duplicate_of:
                continue
            digest = hashlib.sha256(data(doc)).hexdigest()
            self.assertNotIn(digest, seen, f"{doc.path} is identical to {seen.get(digest)}")
            seen[digest] = doc.path

    def test_near_duplicates_are_similar_but_not_equal(self) -> None:
        found = 0
        for doc in DOCS.values():
            if doc.near_duplicate_of:
                found += 1
                other = DOCS[doc.near_duplicate_of]
                a = "\n".join(doc.page_texts()).splitlines()
                b = "\n".join(other.page_texts()).splitlines()
                self.assertNotEqual(a, b, doc.path)
                ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
                self.assertGreater(ratio, 0.5, f"{doc.path}: only {ratio:.2f} of the lines match")
        self.assertGreaterEqual(found, 2)

    def test_text_pdfs_have_a_text_layer_and_scans_do_not(self) -> None:
        for doc in DOCS.values():
            raw = data(doc)
            if doc.kind == "pdf_text":
                self.assertIn(b"Tj", raw, doc.path)
                self.assertNotIn(b"/DCTDecode", raw, doc.path)
                self.assertEqual(raw.count(b"/Type /Page\n"), len(doc.blocks), doc.path)
            if doc.kind == "pdf_scan":
                self.assertNotIn(b" Tj", raw, f"{doc.path} must have no text layer")
                self.assertEqual(raw.count(b"/DCTDecode"), len(doc.lines), doc.path)

    def test_pdf_text_matches_the_ground_truth(self) -> None:
        for doc in DOCS.values():
            if doc.kind != "pdf_text":
                continue
            raw = data(doc).decode("latin-1")
            for block in (b for page in doc.blocks for b in page):
                first_word = block.text.split()[0]
                self.assertIn(f"({first_word}", raw, f"{doc.path}: {block.text[:30]}")

    def test_docx_text_matches_the_ground_truth(self) -> None:
        for doc in DOCS.values():
            if doc.kind != "docx":
                continue
            with zipfile.ZipFile(io.BytesIO(data(doc))) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.namelist()[0], "[Content_Types].xml")
                for info in archive.infolist():
                    self.assertEqual(info.date_time, (2026, 1, 1, 0, 0, 0), "fixed timestamps")
                root = ET.fromstring(archive.read("word/document.xml"))  # noqa: S314 - our own output
            paragraphs = [
                "".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p")
            ]
            self.assertEqual("\n".join(paragraphs), doc.page_texts()[0], doc.path)

    def test_images_open_and_have_a_sensible_size(self) -> None:
        for doc in DOCS.values():
            if doc.kind in ("png", "jpg"):
                with Image.open(io.BytesIO(data(doc))) as image:
                    self.assertEqual(image.format, "PNG" if doc.kind == "png" else "JPEG")
                    self.assertGreater(image.width, 400, doc.path)
                    self.assertGreater(image.height, 200, doc.path)
                self.assertLess(len(data(doc)), 500_000, doc.path)

    def test_drawn_text_never_overlaps_or_leaves_its_picture(self) -> None:
        pens: list[tuple[str, Pen]] = []
        for doc in DOCS.values():
            if doc.kind in ("png", "jpg"):
                pens.append((doc.path, image_layout(doc)))
            if doc.kind == "pdf_scan":
                pens += [
                    (f"{doc.path} page {n}", scan_layout(page))
                    for n, page in enumerate(doc.lines, 1)
                ]
        self.assertGreater(len(pens), 10)
        for where, pen in pens:
            width, height = pen.image.size
            for placed in pen.placed:
                left, top, right, bottom = placed.box
                self.assertTrue(
                    left >= 0 and top >= 0 and right <= width and bottom <= height,
                    f"{where}: {placed.text!r} runs off the picture",
                )
                if placed.within:
                    w_left, w_top, w_right, w_bottom = placed.within
                    self.assertTrue(
                        left >= w_left and right <= w_right and top >= w_top and bottom <= w_bottom,
                        f"{where}: {placed.text!r} leaves its box",
                    )
            for i, one in enumerate(pen.placed):
                for other in pen.placed[i + 1 :]:
                    apart = (
                        one.box[2] <= other.box[0]
                        or other.box[2] <= one.box[0]
                        or one.box[3] <= other.box[1]
                        or other.box[3] <= one.box[1]
                    )
                    self.assertTrue(apart, f"{where}: {one.text!r} overlaps {other.text!r}")

    def test_files_stay_under_the_repository_size_limit(self) -> None:
        for path, content in FILES.items():
            self.assertLess(len(content), 1_000_000, path)

    def test_only_fictional_email_addresses(self) -> None:
        for doc in DOCS.values():
            for address in re.findall(
                r"[\w.+-]+@[\w-]+(?:\.[A-Za-z][\w-]*)+", "\n".join(doc.page_texts())
            ):
                self.assertTrue(address.endswith(".example"), f"{doc.path}: {address}")

    def test_generation_is_deterministic(self) -> None:
        again = build.build_outputs()
        self.assertEqual(again, FILES)

    def test_manifest_matches_the_files(self) -> None:
        manifest = json.loads(FILES["demo/corpus/manifest.json"])
        listed = {e["path"]: e for e in manifest["documents"]}
        self.assertEqual(set(listed), set(DOCS))
        for path, entry in listed.items():
            self.assertEqual(
                entry["sha256"], hashlib.sha256(FILES[f"demo/corpus/{path}"]).hexdigest()
            )
            self.assertEqual(entry["bytes"], len(FILES[f"demo/corpus/{path}"]))


class EvalSetTests(unittest.TestCase):
    def test_size_and_ids(self) -> None:
        self.assertTrue(40 <= len(QUESTIONS) <= 60, len(QUESTIONS))
        ids = [q.id for q in QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({q.question for q in QUESTIONS}), len(QUESTIONS), "unique questions")

    def test_every_category_is_covered(self) -> None:
        by_category: dict[str, int] = {}
        for q in QUESTIONS:
            by_category[q.category] = by_category.get(q.category, 0) + 1
        for category in ("lookup", "multi_doc", "version", "ocr", "numeric", "paraphrase"):
            self.assertGreaterEqual(by_category.get(category, 0), 3, category)
        self.assertGreaterEqual(by_category.get("not_in_vault", 0), 8)

    def test_every_source_quote_is_in_the_document(self) -> None:
        for q in QUESTIONS:
            for src in (*q.sources, *q.alternatives):
                doc = DOCS[src.path]
                texts = doc.page_texts()
                if doc.paginated:
                    self.assertIsNotNone(src.page, f"{q.id}: {src.path} needs a page")
                    assert src.page is not None
                    self.assertTrue(1 <= src.page <= len(texts), f"{q.id}: page {src.page}")
                    haystack = norm(texts[src.page - 1])
                else:
                    self.assertIsNone(src.page, f"{q.id}: {src.path} has no pages")
                    haystack = norm(texts[0])
                self.assertIn(norm(src.quote), haystack, f"{q.id}: {src.quote!r} in {src.path}")

    def test_answerable_questions_have_sources_and_others_have_none(self) -> None:
        for q in QUESTIONS:
            if q.answerable:
                self.assertTrue(q.sources, q.id)
            else:
                self.assertFalse(q.sources, q.id)

    def test_distractors_exist_and_are_not_also_sources(self) -> None:
        for q in QUESTIONS:
            for path in q.distractors:
                self.assertIn(path, DOCS, q.id)
            cited = {s.path for s in (*q.sources, *q.alternatives)}
            self.assertFalse(cited & set(q.distractors), q.id)

    def test_multi_doc_questions_need_several_documents_and_the_rest_need_one(self) -> None:
        for q in QUESTIONS:
            needed = {s.path for s in q.sources}
            if q.category == "multi_doc":
                self.assertGreaterEqual(len(needed), 2, f"{q.id} is answered by one document")
            elif q.answerable:
                self.assertEqual(len(needed), 1, f"{q.id} needs {len(needed)} documents")

    def test_no_single_document_can_answer_a_multi_doc_question(self) -> None:
        # If one document holds every needed passage, a retriever that finds only it answers the
        # question, and the other listed documents would be required for no reason.
        for q in QUESTIONS:
            if q.category != "multi_doc":
                continue
            for path, doc in DOCS.items():
                text = norm(" ".join(doc.page_texts()))
                with self.subTest(question=q.id, document=path):
                    self.assertFalse(all(norm(s.quote) in text for s in q.sources))

    def test_the_documents_of_a_multi_doc_question_do_not_repeat_each_other(self) -> None:
        # The extension email once repeated the syllabus rule, so the email alone answered "did
        # it follow the rule" and the syllabus was required for no reason. A general check on
        # numbers was tried and flagged only incidental matches (a 10 in a grade file, 24 hours in
        # two spec versions), so the known cases are pinned here and the rest is for review.
        email = norm(" ".join(DOCS["emails/advisor-extension-request.txt"].page_texts()))
        self.assertNotIn("48 hours", email)
        timing = next(q for q in QUESTIONS if q.question.startswith("Did the extension request"))
        self.assertEqual(
            {s.path for s in timing.sources},
            {"courses/cse485/syllabus.pdf", "emails/advisor-extension-request.txt"},
        )
        added = next(q for q in QUESTIONS if "were added between version 1" in q.question)
        self.assertEqual(added.category, "multi_doc")
        self.assertIn("project/requirements-v1.docx", {s.path for s in added.sources})

    def test_alternatives_come_from_other_documents(self) -> None:
        for q in QUESTIONS:
            needed = {s.path for s in q.sources}
            for alt in q.alternatives:
                self.assertNotIn(alt.path, needed, q.id)
            if q.alternatives:
                self.assertNotEqual(q.category, "multi_doc", f"{q.id}: every source is needed")

    def test_every_document_that_holds_a_source_quote_is_cited(self) -> None:
        # The other direction of the alternatives rule: a near-duplicate that keeps the answer
        # (the edited standup repeats the SQLite line) answers the question as well as the
        # original, so a retriever that returns it must not be scored as a miss. Exact copies
        # are matched to their original when scoring, so they are skipped. A paraphrase in
        # another document (the journal restating a decision in its own words) is not caught
        # here and is checked in review.
        for q in QUESTIONS:
            cited = {s.path for s in (*q.sources, *q.alternatives)}
            for src in q.sources:
                for path, doc in DOCS.items():
                    if doc.exact_duplicate_of or path in cited:
                        continue
                    with self.subTest(question=q.id, document=path):
                        self.assertNotIn(norm(src.quote), norm(" ".join(doc.page_texts())))

    def test_the_edited_standup_is_an_alternative_for_the_sqlite_question(self) -> None:
        question = next(q for q in QUESTIONS if q.question.startswith("Which database did Jordan"))
        self.assertEqual(
            {a.path for a in question.alternatives}, {"notes/standup-2026-09-15-edited.txt"}
        )

    def test_the_journal_is_an_alternative_for_the_in_app_chat_question(self) -> None:
        question = next(q for q in QUESTIONS if q.question.startswith("Is in-app chat part"))
        self.assertEqual({a.path for a in question.alternatives}, {"notes/project-journal.md"})

    def test_an_alternative_carries_every_number_of_the_expected_answer(self) -> None:
        # An alternative is as good as a source, so it must answer the question on its own. This
        # catches a missing time, date, year or count; a missing fact in words is left to review.
        for q in QUESTIONS:
            for alt in q.alternatives:
                missing = numbers(q.answer) - numbers(alt.quote)
                self.assertFalse(missing, f"{q.id}: {alt.path} lacks {sorted(missing)}")

    def test_the_sources_carry_every_number_of_the_expected_answer(self) -> None:
        # A retriever that finds every listed passage must have found everything the answer says.
        for q in QUESTIONS:
            if q.answerable:
                evidence: set[str] = set()
                for src in q.sources:
                    evidence |= numbers(src.quote)
                missing = numbers(q.answer) - evidence
                with self.subTest(question=q.id):
                    self.assertFalse(missing, f"no source passage has {sorted(missing)}")

    def test_the_number_rule_notices_a_missing_time(self) -> None:
        answer = "Monday, October 12, 2026 at 11:59 PM."
        incomplete = "The deadline was extended to October 12, 2026."
        self.assertEqual(numbers(answer) - numbers(incomplete), {"11:59"})
        self.assertEqual(numbers(answer) - numbers("Monday, October 12, 2026 at 11:59 PM"), set())

    def test_version_questions_name_the_outdated_document(self) -> None:
        for q in QUESTIONS:
            if q.category == "version":
                self.assertTrue(q.distractors, q.id)

    def test_ocr_questions_are_only_answerable_from_pictures_and_scans(self) -> None:
        for q in QUESTIONS:
            if q.category == "ocr":
                for src in q.sources:
                    self.assertIn(DOCS[src.path].kind, ("pdf_scan", "png", "jpg"), q.id)

    def test_not_in_vault_questions_are_really_not_in_the_vault(self) -> None:
        corpus_text = norm(" ".join(" ".join(d.page_texts()) for d in DOCS.values())).lower()
        for term in ("phone", "enrolled", "midterm", "github", "jordan.lee@", "next year"):
            self.assertNotIn(term, corpus_text, term)

    def test_committed_yaml_matches_the_generator(self) -> None:
        self.assertEqual(
            yaml.safe_load(FILES["eval/public/questions.yaml"])["questions"],
            [build.question_entry(q) for q in QUESTIONS],
        )


if __name__ == "__main__":
    unittest.main()
